"""The simulation harness: ground + N fake satellites on a loopback bus under a virtual clock.

    uv run orbit sim --scenario memory_pressure --rounds 50 --seed 42

Time is stepped in fixed increments; every datagram goes through the real codec and the
real ground/satellite code. Because the clock is virtual and every random choice is
seeded, the table this prints is identical on every run for the same seed — which is
what lets five judges see the same demo.
"""

from __future__ import annotations

import io
import json
import logging
from dataclasses import dataclass, field
from typing import Any

from orbit import config, corpus
from orbit.arbiter.fsm import GroundStation
from orbit.bus.loopback import LoopbackBus, LoopbackHub
from orbit.config import Settings
from orbit.ground.stream import EventStream, RunFile
from orbit.ground.telemetry import Telemetry, bus_summary
from orbit.log import log
from orbit.protocol import messages as M
from orbit.sim.satellite import FakeSatellite
from orbit.sim.scenarios import SCENARIOS, Scenario

lg = logging.getLogger("orbit.sim")

GROUND = "ground-sim"
STEP_S = 0.01  # virtual time per step; finer than any timeout in Settings
BUS_LOG_MAX = 20000
SNAPSHOT_S = 1.0  # virtual seconds between snapshot/flags telemetry events


@dataclass
class SlotRow:
    """One line of the Phase C table: who won a slot and exactly why."""

    round_id: int
    t: float
    winner: str
    item_id: int
    score: float
    item_age_term: float
    sat_wait_term: float
    total: float
    runner_up: str
    runner_total: float | None
    margin: float | None
    excluded: list[str]
    outcome: str = "pending"  # complete | failed | revoked
    slots_remaining: int = 0
    sats: dict[str, dict[str, Any]] = field(default_factory=dict)


class Simulation:
    def __init__(self, scenario: Scenario, settings: Settings, seed: int, run_id: str | None = None,
                 write_run: bool = True) -> None:
        self.scenario = scenario
        self.s = settings.with_overrides(**scenario.settings_overrides, seed=seed)
        self.seed = seed
        self.run_id = run_id or f"sim-{scenario.name}-{seed}"
        self.run_file = RunFile(self.s.runs_dir, self.run_id) if write_run else None
        self.stream = EventStream(self.s, self.run_id, writers=[self.run_file] if self.run_file else [], wall=False)
        self.hub = LoopbackHub(seed=seed, faults=scenario.faults)
        self.now = 0.0
        self.events: list[dict[str, Any]] = []
        self.bus_log: list[dict[str, Any]] = []
        self.telemetry = Telemetry(self.s, GROUND, network=False)
        self.telemetry.attach(self.events.append)
        self.telemetry.attach(lambda e: self.stream.on_event(str(e["kind"]), e))
        self.ground = GroundStation(self.s, GROUND, sink=self.telemetry.emit)
        self.ground_bus = LoopbackBus(self.hub, GROUND, self.s.dedup_window, self.s.bus_max_datagram)
        corp = corpus.load()
        self.sats = [FakeSatellite(p, self.s, corp, seed=seed) for p in scenario.profiles]
        self.sat_bus = {sat.hostname: LoopbackBus(self.hub, sat.hostname, self.s.dedup_window, self.s.bus_max_datagram)
                        for sat in self.sats}
        self.rows: list[SlotRow] = []
        self._pending: dict[int, SlotRow] = {}
        self._next_snapshot = 0.0

    # ------------------------------------------------------------------ stepping

    def run(self, rounds: int, max_virtual_s: float = 3600.0) -> None:
        self.stream.start(self.now)
        self._send(self.ground_bus, self.ground.start(self.now))
        for sat in self.sats:
            self._send(self.sat_bus[sat.hostname], sat.start(self.now))
        steps = 0
        while self.now < max_virtual_s:
            self.step()
            steps += 1
            if self.ground.state == config.STATE_CLOSED:
                break
            if len(self.rows) >= rounds and self.ground.state != config.STATE_BUSY:
                break
        self._drain()
        self.stream.end(self.now, reason="window_closed" if not self.ground.window.open else "rounds_done")
        if self.run_file is not None:
            self.run_file.close()
        log(lg, logging.INFO, "sim_done", steps=steps, virtual_s=round(self.now, 2), rounds=len(self.rows),
            state=self.ground.state)

    def _drain(self, cycles: int = 3) -> None:
        """Deliver what is still in flight (typically the last tx_ack) without advancing anyone's timers."""
        for _ in range(cycles):
            self.hub.deliver()
            for msg in self.ground_bus.poll():
                self._log_bus(msg)
                self._observe(msg)
                self._send(self.ground_bus, self.ground.on_message(msg, self.now))
            for sat in self.sats:
                bus = self.sat_bus[sat.hostname]
                for msg in bus.poll():
                    self._send(bus, sat.on_message(msg, self.now))

    def step(self) -> None:
        self.hub.deliver()
        for msg in self.ground_bus.poll():
            self._log_bus(msg)
            self._observe(msg)
            self._send(self.ground_bus, self.ground.on_message(msg, self.now))
        self._send(self.ground_bus, self.ground.on_tick(self.now))
        if self.now >= self._next_snapshot:
            self._next_snapshot = self.now + SNAPSHOT_S
            snap = self.ground.snapshot(self.now)
            self.telemetry.emit("snapshot", {"t": self.now, **snap})
            self.telemetry.emit("flags", {"t": self.now, "sats": {h: s["flags"] for h, s in snap["sats"].items()}})
        for sat in self.sats:
            bus = self.sat_bus[sat.hostname]
            for msg in bus.poll():
                self._send(bus, sat.on_message(msg, self.now))
            self._send(bus, sat.on_tick(self.now))
        self.now = round(self.now + STEP_S, 6)

    def _send(self, bus: LoopbackBus, msgs: list[M.Message]) -> None:
        for m in msgs:
            bus.send(m)
            if bus is self.ground_bus:
                self._log_bus(m)
                self._observe(m)

    def _log_bus(self, msg: M.Message) -> None:
        self.stream.on_bus(msg, self.now, outbound=msg.sender == GROUND)
        entry = {"t": self.now, "dir": "out" if msg.sender == GROUND else "in", **bus_summary(msg)}
        if len(self.bus_log) < BUS_LOG_MAX:
            self.bus_log.append(entry)
        self.telemetry.emit("bus", entry)

    # ------------------------------------------------------------------ table building

    def _observe(self, msg: M.Message) -> None:
        """Turn the ground's own grants/acks/revokes into table rows. Reading the wire, not internals:
        the display will do exactly this from the same messages."""
        if isinstance(msg, M.Grant):
            d = self.ground.decisions[-1]
            ru = d.runner_up
            row = SlotRow(round_id=msg.round_id, t=self.now, winner=msg.to, item_id=msg.item_id,
                          score=msg.breakdown.score, item_age_term=msg.breakdown.item_age_term,
                          sat_wait_term=msg.breakdown.sat_wait_term, total=msg.breakdown.total,
                          runner_up=ru.hostname if ru else "-", runner_total=ru.total if ru else None,
                          margin=d.margin, excluded=sorted(d.excluded), sats=self._sat_columns())
            self._pending[msg.round_id] = row
            self.rows.append(row)
        elif isinstance(msg, M.TxAck):
            done = self._pending.pop(msg.round_id, None)
            if done is not None:
                done.outcome = "complete" if msg.ok else "failed"
                done.slots_remaining = self.ground.window.slots_remaining
        elif isinstance(msg, M.Revoke):
            gone = self._pending.pop(msg.round_id, None)
            if gone is not None:
                gone.outcome = f"revoked:{msg.reason}"
                gone.slots_remaining = self.ground.window.slots_remaining

    def _sat_columns(self) -> dict[str, dict[str, Any]]:
        out: dict[str, dict[str, Any]] = {}
        flags = self.ground.assessments(self.now)
        for sat in self.sats:
            a = flags.get(sat.hostname)
            out[sat.hostname] = dict(occ=sat.buffer.stats().occupancy_pct, qlen=len(sat.queue),
                                     ev=sat.counters.evicted + sat.counters.rejected,
                                     flag=_flag_str(a.kind, a.level, a.memory_pressured) if a else "-")
        return out

    # ------------------------------------------------------------------ output

    def table(self) -> str:
        o = io.StringIO()
        w = o.write
        hosts = [s.hostname for s in self.sats]
        w(f"scenario: {self.scenario.name} — {self.scenario.description}\n")
        w(f"seed={self.seed}  ITEM_AGING_RATE={self.s.item_aging_rate}  SAT_AGING_RATE={self.s.sat_aging_rate}  "
          f"window={self.s.window_slots} slots ({self.s.window_duration_s:.0f}s @ {self.s.link_rate_bps:.0f} bps, "
          f"scaled)  buffer={self.s.sat_buffer_slots} slots/sat  faults={vars(self.scenario.faults)}\n\n")
        head = (f"{'rnd':>3} {'t':>6}  {'winner':<6} {'item':>5}  {'score':>6} {'+age':>6} {'+wait':>6} "
                f"{'=prio':>7}  {'runner-up':<14} {'margin':>6}  {'outcome':<18} {'left':>4}  ")
        head += "  ".join(f"{h:<22}" for h in hosts)
        w(head + "\n")
        w(f"{'':>3} {'':>6}  {'':<6} {'':>5}  {'':>6} {'':>6} {'':>6} {'':>7}  {'':<14} {'':>6}  "
          f"{'':<18} {'':>4}  ")
        w("  ".join(f"{'occ%  q  ev  flag':<22}" for _ in hosts) + "\n")
        w("-" * len(head) + "\n")
        for r in self.rows:
            ru = f"{r.runner_up} {r.runner_total:.1f}" if r.runner_total is not None else "-"
            mg = f"{r.margin:+.1f}" if r.margin is not None else ""
            line = (f"{r.round_id:>3} {r.t:>6.1f}  {r.winner:<6} {r.item_id:>5}  {r.score:>6.1f} "
                    f"{r.item_age_term:>6.1f} {r.sat_wait_term:>6.1f} {r.total:>7.1f}  {ru:<14} {mg:>6}  "
                    f"{r.outcome:<18} {r.slots_remaining:>4}  ")
            cols = []
            for h in hosts:
                c = r.sats.get(h)
                cols.append(f"{c['occ']:>4.0f} {c['qlen']:>2} {c['ev']:>3}  {c['flag']:<10}" if c
                            else f"{'offline':<22}")
            w(line + "  ".join(cols) + "\n")
        w("\n")
        # per-satellite summary
        w(f"{'satellite':<8} {'captured':>8} {'granted':>7} {'sent':>5} {'evicted':>7} {'rejected':>8} {'failed':>6} "
          f"{'revoked':>7} {'queue':>5} {'occ%':>5}  {'wait_s':>6}  flag\n")
        flags = self.ground.assessments(self.now)
        for sat in self.sats:
            cnt = sat.counters
            a = flags.get(sat.hostname)
            w(f"{sat.hostname:<8} {cnt.captured:>8} {cnt.grants:>7} {cnt.transmitted:>5} {cnt.evicted:>7} "
              f"{cnt.rejected:>8} {cnt.failed:>6} {cnt.revoked:>7} {len(sat.queue):>5} "
              f"{sat.buffer.stats().occupancy_pct:>5.0f}  "
              f"{a.wait_s if a else 0:>6.1f}  {a.reason if a else '-'}\n")
        w("\n")
        g = self.ground
        gc = g.counters
        w(f"ground: state={g.state} rounds={gc.rounds} grants={gc.grants} completed={gc.completed} "
          f"revoked={gc.revoked} failed_tx={gc.failed_tx} no_bid_rounds={gc.no_bid_rounds} "
          f"late_bids={gc.late_bids} unexpected={gc.unexpected}\n")
        ws = g.window.snapshot()
        w(f"window: {ws['slots_used']}/{ws['slots_total']} slots used, {ws['remaining_bytes']} bytes left, "
          f"open={ws['open']}\n")
        b = self.ground_bus.stats
        w(f"ground bus: received={b.received} delivered={b.delivered} dup={b.dropped_dup} "
          f"malformed={b.dropped_malformed} own={b.dropped_own}  hub datagrams={self.hub.datagrams}\n")
        w(f"telemetry events: {len(self.events)}  bus log entries: {len(self.bus_log)}  "
          f"virtual time: {self.now:.1f}s\n")
        st = self.stream
        w(f"event stream: {st.seq} events → {self.run_file.path if self.run_file else '(not written)'}  "
          f"orbit usable {st.orbit_usable_down}/{st.orbit_frames_down}  "
          f"FIFO baseline usable {st.baseline.usable_down}/{st.baseline.frames_down} "
          f"(dropped full {st.baseline.dropped})\n")
        # value delivered: sum of scores of transmitted frames, the number the pitch compares
        delivered = sum(r.score for r in self.rows if r.outcome == "complete")
        n_ok = sum(r.outcome == "complete" for r in self.rows)
        w(f"value delivered (sum of transmitted scores): {delivered:.1f} over {n_ok} frames\n")
        return o.getvalue()

    def digest(self) -> str:
        """A stable fingerprint of the run for the determinism check."""
        import hashlib
        h = hashlib.sha256()
        for r in self.rows:
            h.update(f"{r.round_id}|{r.winner}|{r.item_id}|{r.total:.4f}|{r.outcome}".encode())
        return h.hexdigest()[:16]


def _flag_str(kind: object, level: object, pressured: bool) -> str:
    k, lv = str(kind), str(level)
    base = {"nominal": "ok", "starved": f"starved/{lv}", "idle": "idle", "never_transmitted": f"new/{lv}",
            "silent": "SILENT"}.get(k, k)
    return base + ("+MEM" if pressured else "")


def run_scenario(name: str, rounds: int, seed: int, settings: Settings | None = None, run_id: str | None = None,
                 write_run: bool = True) -> Simulation:
    sim = Simulation(SCENARIOS[name], settings or config.Settings.from_env(), seed, run_id=run_id, write_run=write_run)
    sim.run(rounds)
    return sim


def main(argv: list[str] | None = None) -> int:
    import argparse
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--scenario", default="nominal", choices=sorted(SCENARIOS))
    ap.add_argument("--rounds", type=int, default=50)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--item-aging-rate", type=float, default=None)
    ap.add_argument("--sat-aging-rate", type=float, default=None)
    ap.add_argument("--events", default=None, help="write all telemetry events as JSON lines to this path")
    ap.add_argument("--digest", action="store_true", help="print only the run digest (determinism check)")
    ap.add_argument("--run-id", default=None)
    ap.add_argument("--no-runs", action="store_true")
    a = ap.parse_args(argv)
    s = config.Settings.from_env().with_overrides(item_aging_rate=a.item_aging_rate, sat_aging_rate=a.sat_aging_rate)
    sim = run_scenario(a.scenario, a.rounds, a.seed, s, run_id=a.run_id, write_run=not a.no_runs)
    if a.events:
        with open(a.events, "w") as f:
            for e in sim.events:
                f.write(json.dumps(e, default=str) + "\n")
    if a.digest:
        print(sim.digest())
    else:
        print(sim.table())
        print(f"digest: {sim.digest()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
