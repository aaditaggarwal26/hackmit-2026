"""The orchestrator: ground segment above N edge nodes (protocol.md §6).

A run is `passes` orbits. Each pass: an ingest phase (every satellite captures
`frames_per_pass` frames: CONFIG/REF/FRAME rows over the wire, FRAME_SCORED back),
then a contact window (STATUS_QUERY all -> arbitrate -> GRANT winner -> TX_DONE ->
debit the byte budget, until the window is spent or every queue is empty).

The ground mirrors every node's queue from FRAME_SCORED/TX_FRAME and re-scores every
frame with the golden model, so any divergence between a board and the model shows
up live as `mismatches`. The unfiltered baseline (FIFO, round-robin, no scoring)
runs on the same captures and the same window in Python; the gap is the pitch.

Time: `clock()` returns milliseconds. Wall clock under the server; a virtual clock
(advanced instead of sleeping) for headless runs and tests, installed into
SimulatedNodes so their timers agree with ours. Nodes are polled with recv(0),
never blocking, so one loop serves sim, Verilator and hardware transports.

    uv run python -m orbit.orchestrator.main --scenario nominal --nodes sim://0 sim://1 --passes 2
"""
from __future__ import annotations

import argparse
import asyncio
import time
from collections import Counter, deque
from dataclasses import asdict, dataclass, field
from typing import Callable

from orbit import params
from orbit.golden.node import SimulatedNode, rows
from orbit.golden.queue import NO_FRAME, PriorityQueue
from orbit.golden.score import Config, display, score_frame
from orbit.orchestrator.arbitration import Arbiter, Candidate
from orbit.orchestrator.source import Capture, CorpusSource
from orbit.orchestrator.window import ContactWindow
from orbit.protocol import messages as M
from orbit.protocol.messages import FrameDecoder, encode
from orbit.protocol.transport import open_transport

STATE_NAMES = {M.StatusReply.IDLE: "IDLE", M.StatusReply.SCORING: "SCORING", M.StatusReply.BENCH: "BENCH"}
RING = 200
PERIODIC = {"FrameIngest", "RefFrameSet", "Heartbeat", "Power", "StatusReply", "StatusQuery"}


def msg_dict(m: M.Message) -> dict:
    d = asdict(m)
    d.pop("pixels", None)
    d["type"] = type(m).__name__
    return d


def power_dict(p: M.Power) -> dict:
    """INA219 raw registers -> volts/amps; the only place this arithmetic lives."""
    if not p.flags & M.Power.VALID:
        return dict(valid=False, uptime_ms=p.uptime_ms)
    bus_v = (p.bus_raw >> 3) * params.INA219_BUS_LSB_V
    shunt_v = p.shunt_raw * params.INA219_SHUNT_LSB_V
    amps = shunt_v / params.INA219_SHUNT_OHM
    return dict(valid=True, bus_v=bus_v, shunt_mv=shunt_v * 1e3, amps=amps, watts=bus_v * amps, uptime_ms=p.uptime_ms)


@dataclass
class NodeState:
    idx: int
    path: str
    node_id: int | None = None
    simulated: bool = False
    status: dict | None = None
    power: dict | None = None
    rx_counts: Counter = field(default_factory=Counter)
    last_rx_ms: int | None = None
    uptime_ms: int | None = None
    ref_id: int | None = None
    captures: int = 0
    last_capture: dict | None = None
    scored: list[dict] = field(default_factory=list)        # FRAME_SCORED records, newest last
    mirror: PriorityQueue = field(default_factory=PriorityQueue)
    sent: list[dict] = field(default_factory=list)          # TX_FRAME records
    slots_won: int = 0                                      # this window
    slots_won_total: int = 0
    mismatches: int = 0                                     # board vs golden disagreements (should stay 0)
    timeouts: int = 0
    tx_done: dict | None = None
    rx_log: list = field(default_factory=list)              # non-periodic messages, in order (E2E comparisons)

    @property
    def link_ok(self) -> bool:
        return bool(self.status and self.status["flags"] & M.StatusReply.F_LINK_OK)

    def snapshot(self) -> dict:
        st = self.status
        f = st["flags"] if st else 0
        S = M.StatusReply
        return dict(
            idx=self.idx, path=self.path, node_id=self.node_id, simulated=self.simulated,
            state=STATE_NAMES.get(st["state"], "?") if st else "NO CONTACT",
            has_data=bool(f & S.F_HAS_DATA), link_ok=bool(f & S.F_LINK_OK), config_valid=bool(f & S.F_CONFIG_VALID),
            ref_loaded=bool(f & S.F_REF_LOADED), busy=bool(f & S.F_BUSY), tx_stalled=bool(f & S.F_TX_STALLED),
            ina219_present=bool(f & S.F_INA219_PRESENT), status=st, power=self.power,
            top_score=st["top_score"] if st else None, top_frame_id=st["top_frame_id"] if st else None,
            queue_depth=st["queue_depth"] if st else None, queue=[dict(score=s, frame_id=i) for s, i in self.mirror.cells],
            captures=self.captures, last_capture=self.last_capture, ref_id=self.ref_id,
            scored=self.scored[-8:], sent=self.sent[-12:], frames_sent=len(self.sent),
            evicted=self.mirror.evicted, slots_won=self.slots_won, slots_won_total=self.slots_won_total,
            mismatches=self.mismatches, timeouts=self.timeouts, rx_counts=dict(self.rx_counts),
            last_rx_ms=self.last_rx_ms, uptime_ms=self.uptime_ms,
        )


class Orchestrator:
    def __init__(self, nodes: list[str] = params.NODES, source: CorpusSource | None = None, scenario: str = "custom",
                 window: ContactWindow | None = None, frames_per_pass: int = 12, passes: int = 4,
                 starvation_n: int = params.STARVATION_N, config: Config = Config(),
                 clock: Callable[[], float] | None = None, virtual: bool = False, speed: float = 1.0,
                 capture_ms: int = 300, slot_ms: int = 400, reply_timeout_ms: int = 5000, pacing_source: str = ""):
        self.source = source if source is not None else CorpusSource([[] for _ in nodes])
        self.scenario, self.pacing_source = scenario, pacing_source
        self.window = window or ContactWindow()
        self.frames_per_pass, self.n_passes = frames_per_pass, passes
        self.arbiter = Arbiter(starvation_n)
        self.config = config
        self.virtual = virtual
        self._vclock = 0.0
        if clock is None:
            clock = (lambda: self._vclock) if virtual else (lambda: time.monotonic() * 1000.0)
        self.clock = clock
        self.t_start = clock()
        self.chunk_ms = params.POWER_PERIOD_MS if virtual else 20
        self.speed = speed
        self.capture_ms, self.slot_ms, self.reply_timeout_ms = capture_ms, slot_ms, reply_timeout_ms
        self.pass_idx, self.phase = -1, "idle"
        self.slot_id = 0
        self.paused, self.step_requested, self.done, self.running = False, False, False, False
        self.transports = [open_transport(p, params.BAUD) for p in nodes]
        self.nodes = [NodeState(i, p, node_id=t.node_id, simulated=isinstance(t, SimulatedNode))
                      for i, (p, t) in enumerate(zip(nodes, self.transports))]
        for t in self.transports:
            if isinstance(t, SimulatedNode):   # sim only: share our clock so tests do not sleep
                t.clock, t.t0 = self.clock, self.clock()
        self.decoders = [FrameDecoder() for _ in nodes]
        self.rx_buf = [bytearray() for _ in nodes]
        self.frames_tx: deque = deque(maxlen=RING)
        self.frames_rx: deque = deque(maxlen=RING)
        self.notable_tx: deque = deque(maxlen=RING)
        self.notable_rx: deque = deque(maxlen=RING)
        self.wire: deque = deque(maxlen=4000)            # (t_ms, tx_bytes, rx_bytes) for the wire-rate indicator
        self.hb_seq = 0
        self.next_hb_ms = 0
        self.slots: list[dict] = []                       # every grant, all passes
        self.history: list[dict] = []                     # per pass
        self.value_filtered = 0
        self.value_fifo = 0
        self.fifo: list[deque] = [deque() for _ in nodes]
        self.fifo_dropped = 0
        self.fifo_rr = 0
        self.fifo_sent: list[dict] = []
        self.subscribers: list[asyncio.Queue] = []
        self.events: list[dict] = []

    # --- clock / pacing -------------------------------------------------------------
    def now_ms(self) -> int:
        return int(self.clock() - self.t_start)

    async def _sleep_ms(self, ms: float) -> None:
        if self.virtual:
            self._vclock += ms
            await asyncio.sleep(0)
        else:
            await asyncio.sleep(ms / 1000)

    async def _idle(self, until_ms: int, stop: Callable[[], bool] | None = None) -> bool:
        """Poll until `stop()` or the deadline; True if stopped early."""
        while True:
            self._poll_all()
            self._heartbeats()
            if stop and stop():
                return True
            if self.now_ms() >= until_ms:
                return False
            await self._sleep_ms(min(self.chunk_ms, until_ms - self.now_ms()))

    async def _pace(self, ms: int) -> None:
        await self._idle(self.now_ms() + int(ms / self.speed))

    async def _wait_paused(self) -> None:
        while self.paused and not self.step_requested:
            await self._idle(self.now_ms() + self.chunk_ms)
        self.step_requested = False

    # --- events -----------------------------------------------------------------------
    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.append(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.remove(q)

    def _emit(self, event: str, **data) -> None:
        evt = dict(type="event", event=event, t_ms=self.now_ms(), **data)
        if len(self.events) < 20_000:
            self.events.append(evt)
        for q in self.subscribers:
            q.put_nowait(evt)

    # --- wire ------------------------------------------------------------------------
    def _send(self, i: int, msg: M.Message) -> None:
        frame = encode(msg)
        self.transports[i].send(frame)
        self.wire.append((self.now_ms(), len(frame), 0))
        entry = dict(t_ms=self.now_ms(), way="tx", node=i, hex=frame.hex(" ") if len(frame) < 40 else f"{len(frame)} B",
                     **msg_dict(msg))
        self.frames_tx.append(entry)
        if entry["type"] not in PERIODIC:
            self.notable_tx.append(entry)

    def _poll(self, i: int) -> None:
        t, buf = self.transports[i], self.rx_buf[i]
        while (chunk := t.recv(0.0)) is not None:
            buf += chunk
            self.wire.append((self.now_ms(), 0, len(chunk)))
        while (end := buf.find(0)) >= 0:
            raw, buf[:] = bytes(buf[:end + 1]), buf[end + 1:]
            msgs = self.decoders[i].feed(raw)
            if len(raw) < 4:      # link-open / resync delimiter
                continue
            for m in msgs or [None]:
                entry = dict(t_ms=self.now_ms(), way="rx", node=i, hex=raw.hex(" "))
                entry.update(msg_dict(m) if m else dict(type="DROPPED"))
                self.frames_rx.append(entry)
                if entry["type"] not in PERIODIC:
                    self.notable_rx.append(entry)
                if m:
                    if entry["type"] not in PERIODIC:
                        self.nodes[i].rx_log.append(m)
                    self._handle(i, m)

    def _poll_all(self) -> None:
        for i in range(len(self.nodes)):
            self._poll(i)

    def _heartbeats(self) -> None:
        if self.now_ms() < self.next_hb_ms:
            return
        self.next_hb_ms = self.now_ms() + params.HEARTBEAT_MS
        hb = M.Heartbeat(sender=params.ORCH_ID, seq=self.hb_seq, uptime_ms=self.now_ms() & 0xFFFFFFFF)
        self.hb_seq = (self.hb_seq + 1) & 0xFFFF
        for i in range(len(self.nodes)):
            self._send(i, hb)

    def wire_rate(self, window_ms: int = 1000) -> dict:
        t = self.now_ms() - window_ms
        tx = sum(b for ts, b, _ in self.wire if ts >= t)
        rx = sum(b for ts, _, b in self.wire if ts >= t)
        return dict(tx_bps=tx * 8 * 1000 / window_ms, rx_bps=rx * 8 * 1000 / window_ms, baud=params.BAUD)

    def _handle(self, i: int, m: M.Message) -> None:
        n = self.nodes[i]
        n.rx_counts[type(m).__name__] += 1
        n.last_rx_ms = self.now_ms()
        if isinstance(m, M.StatusReply):
            prev = n.status
            n.status = asdict(m)
            n.node_id = m.node_id
            if prev is None or (prev["state"], prev["flags"]) != (m.state, m.flags):
                self._emit("status", node=i, status=n.snapshot())
            # live check: the board's head must be our mirror's head
            if (m.top_score, m.top_frame_id) != n.mirror.top or m.queue_depth != len(n.mirror):
                n.mismatches += 1
        elif isinstance(m, M.Heartbeat):
            n.node_id, n.uptime_ms = m.sender, m.uptime_ms
        elif isinstance(m, M.Power):
            n.power, n.uptime_ms = power_dict(m), m.uptime_ms
        elif isinstance(m, M.FrameScored):
            rec = msg_dict(m)
            cap = n.last_capture or {}
            exp = cap.get("expected") if cap.get("frame_id") == m.frame_id else None
            lost = n.mirror.insert(m.score, m.frame_id)
            rec.update(pass_idx=self.pass_idx, scene=cap.get("scene"), ref_id=cap.get("ref_id"), expected=exp,
                       display=display(m.score), ok=(exp is None or exp == (m.clear, m.sharp, m.change, m.score))
                       and lost == m.evicted_id and len(n.mirror) == m.queue_depth)
            if not rec["ok"]:
                n.mismatches += 1
            n.scored.append(rec)
            self._emit("scored", node=i, scored=rec)
        elif isinstance(m, M.TxFrame):
            rec = msg_dict(m)
            rec.update(pass_idx=self.pass_idx, display=display(m.score))
            if n.mirror.has_data and n.mirror.top[1] == m.frame_id:
                n.mirror.pop()
            else:
                n.mismatches += 1
            n.sent.append(rec)
            self.value_filtered += m.score
        elif isinstance(m, M.TxDone):
            n.tx_done = msg_dict(m)

    # --- controls ----------------------------------------------------------------------
    def play(self) -> None:
        self.paused = False

    def pause(self) -> None:
        self.paused = True

    def step(self) -> None:
        self.paused, self.step_requested = True, True

    def set_speed(self, x: float) -> None:
        self.speed = min(16.0, max(0.25, float(x)))

    def set_starvation(self, n: int) -> None:
        self.arbiter.starvation_n = max(1, int(n))

    # --- the run ------------------------------------------------------------------------
    async def run(self, passes: int | None = None) -> None:
        self.running = True
        self.next_hb_ms = 0
        try:
            await self._setup()
            for p in range(self.n_passes if passes is None else passes):
                self.pass_idx = p
                await self._ingest_phase(p)
                await self._window_phase(p)
                self._record_pass(p)
            self.phase = "idle"
            self.done = True
            self._emit("done", pass_idx=self.pass_idx)
        finally:
            self.running = False

    def _config_msg(self) -> M.ConfigSet:
        c = self.config
        return M.ConfigSet(w_clear=c.w_clear, w_sharp=c.w_sharp, w_change=c.w_change, cloud_thr=c.cloud_thr,
                           change_thr=c.change_thr, sharp_shift=c.sharp_shift, queue_limit=c.queue_limit)

    async def _setup(self) -> None:
        self.phase = "setup"
        self._heartbeats()
        for i, n in enumerate(self.nodes):
            n.mirror = PriorityQueue(self.config.queue_limit)
            n.status = None
            self._send(i, self._config_msg())
            self._emit("config", node=i, config=asdict(self._config_msg()))
        for i, n in enumerate(self.nodes):
            if not await self._idle(self.now_ms() + self.reply_timeout_ms, stop=lambda: n.status is not None):
                n.timeouts += 1

    async def _send_frame(self, i: int, cap: Capture) -> None:
        n = self.nodes[i]
        if n.ref_id != cap.ref_id:
            for m in rows(cap.ref, cap.ref_id, M.RefFrameSet):
                self._send(i, m)
            n.ref_id = cap.ref_id
            self._emit("ref", node=i, ref_id=cap.ref_id, scene=cap.scene)
        exp = score_frame(cap.frame, cap.ref, self.config)
        n.last_capture = dict(frame_id=cap.frame_id, scene=cap.scene, ref_id=cap.ref_id, index=cap.index,
                              expected=(exp.clear, exp.sharp, exp.change, exp.score), expected_display=display(exp.score),
                              t_ms=self.now_ms())
        n.captures += 1
        for m in rows(cap.frame, cap.frame_id, M.FrameIngest):
            self._send(i, m)
        self._emit("capture", node=i, capture=n.last_capture)
        # unfiltered baseline sees the same capture: FIFO of the same depth, newest dropped when full
        if len(self.fifo[i]) < self.config.queue_limit:
            self.fifo[i].append((cap.frame_id, exp.score))
        else:
            self.fifo_dropped += 1

    async def _ingest_phase(self, p: int) -> None:
        self.phase = "ingest"
        self._emit("pass_start", pass_idx=p, frames_per_pass=self.frames_per_pass)
        for k in range(self.frames_per_pass):
            await self._wait_paused()
            before = [len(n.scored) for n in self.nodes]
            sent = []
            for i in range(len(self.nodes)):
                cap = self.source.next_capture(i)
                if cap is None:
                    continue
                await self._send_frame(i, cap)
                sent.append(i)
            for i in sent:   # FRAME_SCORED per node; hardware needs ~1.5 s per frame at 115200 baud
                n = self.nodes[i]
                if not await self._idle(self.now_ms() + self.reply_timeout_ms, stop=lambda: len(n.scored) > before[i]):
                    n.timeouts += 1
            await self._pace(self.capture_ms)

    async def _query_status(self) -> list[Candidate]:
        seen = [len(n.rx_counts and [1]) and n.rx_counts["StatusReply"] for n in self.nodes]
        for i in range(len(self.nodes)):
            self._send(i, M.StatusQuery())
        await self._idle(self.now_ms() + self.reply_timeout_ms,
                         stop=lambda: all(n.rx_counts["StatusReply"] > seen[i] for i, n in enumerate(self.nodes)))
        cands = []
        for i, n in enumerate(self.nodes):
            if n.rx_counts["StatusReply"] <= seen[i]:
                n.timeouts += 1
            st = n.status or {}
            cands.append(Candidate(i, bool(st.get("flags", 0) & M.StatusReply.F_HAS_DATA), st.get("top_score", 0),
                                   st.get("top_frame_id", NO_FRAME)))
        return cands

    async def _window_phase(self, p: int) -> None:
        self.phase = "window"
        self.window.reset()
        self.arbiter.reset()
        for n in self.nodes:
            n.slots_won = 0
        self._emit("window_open", pass_idx=p, window=self.window.snapshot())
        while self.window.open:
            await self._wait_paused()
            cands = await self._query_status()
            d = self.arbiter.decide(cands)
            if d.winner is None:
                break
            i = d.winner
            n = self.nodes[i]
            self.slot_id = (self.slot_id + 1) & 0xFFFF
            n.tx_done = None
            self._send(i, M.Grant(slot_id=self.slot_id, budget_bytes=self.window.remaining_bytes))
            if not await self._idle(self.now_ms() + self.reply_timeout_ms, stop=lambda: n.tx_done is not None):
                n.timeouts += 1
                break
            consumed = n.tx_done["bytes_consumed"]
            self.window.debit(min(consumed, self.window.remaining_bytes))
            n.slots_won += 1
            n.slots_won_total += 1
            fifo = self._fifo_slot()
            tx = n.sent[-1] if n.sent and n.sent[-1]["slot_id"] == self.slot_id else None
            rec = dict(slot_id=self.slot_id, pass_idx=p, node=i, reason=d.reason, streak=d.streak,
                       runner_up=d.runner_up, candidates=[c.__dict__ for c in d.candidates],
                       frame_id=tx["frame_id"] if tx else None, score=tx["score"] if tx else 0,
                       display=tx["display"] if tx else 0.0, bytes=consumed, new_top_score=n.tx_done["new_top_score"],
                       fifo=fifo, value_filtered=self.value_filtered, value_fifo=self.value_fifo,
                       window=self.window.snapshot(), t_ms=self.now_ms())
            self.slots.append(rec)
            self.window.grants.append(rec)
            self._emit("slot", slot=rec)
            await self._pace(self.slot_ms)
        self._emit("window_close", pass_idx=p, window=self.window.snapshot())

    def _fifo_slot(self) -> dict | None:
        """The unfiltered baseline's use of the same slot: round-robin over satellites, oldest frame first."""
        for _ in range(len(self.nodes)):
            i = self.fifo_rr
            self.fifo_rr = (self.fifo_rr + 1) % len(self.nodes)
            if self.fifo[i]:
                fid, score = self.fifo[i].popleft()
                self.value_fifo += score
                rec = dict(node=i, frame_id=fid, score=score, display=display(score))
                self.fifo_sent.append(rec)
                return rec
        return None

    def _record_pass(self, p: int) -> None:
        entry = dict(pass_idx=p, slots=[s for s in self.slots if s["pass_idx"] == p],
                     value_filtered=self.value_filtered, value_fifo=self.value_fifo,
                     window=self.window.snapshot(), starvation_switches=self.arbiter.starvation_switches,
                     nodes=[dict(captures=n.captures, frames_sent=len(n.sent), evicted=n.mirror.evicted,
                                 slots_won=n.slots_won, queue_depth=len(n.mirror), mismatches=n.mismatches)
                            for n in self.nodes],
                     fifo_dropped=self.fifo_dropped)
        self.history.append(entry)
        self._emit("pass", **entry)

    # --- views -------------------------------------------------------------------------
    def snapshot(self) -> dict:
        return dict(
            type="snapshot", scenario=self.scenario, pass_idx=self.pass_idx, n_passes=self.n_passes, phase=self.phase,
            frames_per_pass=self.frames_per_pass, speed=self.speed, paused=self.paused, running=self.running,
            done=self.done, uptime_ms=self.now_ms(), starvation_n=self.arbiter.starvation_n,
            starvation_switches=self.arbiter.starvation_switches, pacing_source=self.pacing_source,
            config=asdict(self._config_msg()), window=self.window.snapshot(), wire=self.wire_rate(),
            arbitration=self.arbiter.history[-1].as_dict() if self.arbiter.history else None,
            slots=self.slots[-40:], n_slots=len(self.slots),
            value=dict(filtered=self.value_filtered, fifo=self.value_fifo,
                       filtered_display=display(self.value_filtered), fifo_display=display(self.value_fifo),
                       fifo_dropped=self.fifo_dropped, fifo_sent=self.fifo_sent[-12:]),
            history=[dict(pass_idx=h["pass_idx"], value_filtered=h["value_filtered"], value_fifo=h["value_fifo"],
                          slots=len(h["slots"]), starvation_switches=h["starvation_switches"]) for h in self.history],
            nodes=[n.snapshot() for n in self.nodes],
            provenance=self.source.meta(),
            frames=dict(tx=list(self.frames_tx)[-60:], rx=list(self.frames_rx)[-60:],
                        notable_tx=list(self.notable_tx)[-60:], notable_rx=list(self.notable_rx)[-60:]),
        )

    def summary(self) -> str:
        lines = [f"scenario={self.scenario} passes={len(self.history)} slots={len(self.slots)} "
                 f"value filtered={self.value_filtered} fifo={self.value_fifo} "
                 f"gain={self.value_filtered / self.value_fifo if self.value_fifo else float('nan'):.2f}x "
                 f"starvation_switches={self.arbiter.starvation_switches} window={self.window.slots_total} slots/pass"]
        for n in self.nodes:
            lines.append(f"node {n.idx} ({n.path}) id={n.node_id} state={n.snapshot()['state']} link_ok={n.link_ok} "
                         f"captures={n.captures} scored={len(n.scored)} sent={len(n.sent)} evicted={n.mirror.evicted} "
                         f"queue={len(n.mirror)} slots_won={n.slots_won_total} mismatches={n.mismatches} "
                         f"timeouts={n.timeouts} rx={dict(n.rx_counts)}")
        for h in self.history:
            lines.append(f"pass {h['pass_idx']} slots={len(h['slots'])} winners={[s['node'] for s in h['slots']]} "
                         f"reasons={Counter(s['reason'] for s in h['slots'])} filtered={h['value_filtered']} fifo={h['value_fifo']}")
        return "\n".join(lines)

    def close(self) -> None:
        for t in self.transports:
            t.close()


def main(argv=None) -> int:
    from orbit.orchestrator import scenarios
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="nominal", choices=sorted(scenarios.SCENARIOS))
    ap.add_argument("--nodes", nargs="+", default=params.NODES)
    ap.add_argument("--passes", type=int, default=None)
    ap.add_argument("--speed", type=float, default=8.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--starvation", type=int, default=params.STARVATION_N)
    args = ap.parse_args(argv)
    orch = scenarios.build(args.scenario, nodes=args.nodes, seed=args.seed, passes=args.passes,
                           starvation_n=args.starvation, speed=args.speed,
                           virtual=all(n.startswith("sim://") for n in args.nodes))
    asyncio.run(orch.run())
    print(orch.summary())
    orch.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
