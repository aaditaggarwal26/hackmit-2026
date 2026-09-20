#!/usr/bin/env python3
r"""Generate a SYNTHETIC run file: no hardware, no ground station, no radio was involved.

Every event it writes is invented by the code below. Nothing here ever touched an
ESP32, a radio or a serial port, so every node it emits carries real=false and the
transport the ground uses for simulated satellites. Do not re-introduce a hardware
claim: display/live.html renders a green "real board" chip straight off that flag,
and PRODUCT.md requires the display to say which satellites are real at all times.

    uv run python tools/make_sample.py                         # writes runs/sample.jsonl
    uv run python tools/make_sample.py --seed 4 -o runs/x.jsonl

The two committed fixtures are regenerated with exactly these two commands:

    uv run python tools/make_sample.py
    uv run python tools/make_sample.py --seed 39 --run-id demo-2026-09-19T21-00-00 \
        -o runs/demo-2026-09-19T21-00-00.jsonl

Fixture generator, NOT the ground station: it fakes the stream in docs/event_stream.md
by walking three imaginary satellites and one imaginary arbiter through a contact
window, then checks its own output with tools/check_run.py before writing.
Needs numpy (the repo's env, `uv sync`) because the frames are real: corpus ids,
scored with the golden model against their scene references, so the thumbnails
the display looks up (corpus/png/<id>.png) match the scores in the stream.

What the default run (seed 8) contains
  - 3 nodes, named after the simulator's satellites. sat-b flies clear scenes and
    holds the best frames; sat-c flies cloudy ones and rarely wins; sat-a is in
    between
  - ~40 frame_scored, exactly 16 frame_arrived (the byte budget is 16 frames)
  - one grant with reason starvation_forced, so the display has one to draw. The
    fixture triggers it off three wins in a row; the real ground emits that reason
    whenever the aging terms rather than the raw score decided the slot
    (orbit/ground/stream.py). Same reason code, reached a simpler way.
  - sat-c drops off the link for ~13 s, is timed out (warn, then error, then a
    node_status with link_ok=false), stays silent, and returns with the
    frame_scored reports it buffered while disconnected
  - a CRC warning on sat-a, boot infos, link restored info
  - queue_limit 8 (the contract example says 32) so eviction and rejection happen
"""

from __future__ import annotations

import argparse
import datetime as dt
import heapq
import json
import random
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

Event = dict[str, Any]
Frame = dict[str, Any]

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))

from tools import check_run  # noqa: E402

FRAME_BYTES = 16384
SLOTS = 16
BUDGET = SLOTS * FRAME_BYTES
DURATION = 120.0
QUEUE_LIMIT = 8
STARVATION_N = 3
USABLE_MAX = 0.35
SCORING = {"w_clear": 21845, "w_sharp": 21845, "w_change": 21845, "cloud_thr": 200, "change_thr": 16}
POLL_S = 1.0
ARB_TICK_S = 0.25
LINK_TIMEOUT_S = 3.0
PRELOAD = 16
WALL0 = dt.datetime(2026, 9, 19, 20, 30, 0, tzinfo=dt.UTC)
RUN_ID = "sample-2026-09-19T20-30-00"
# What the ground reports for a simulated satellite: orbit/ground/stream.py:218 builds it from
# Settings.mcast_group/mcast_port, and docs/event_stream.md:49-51 shows it in the contract.
# These nodes are software, so this is the honest transport for them.
TRANSPORT = "udp-multicast://239.255.42.99:50000"

NODE_PLAN: list[dict[str, Any]] = [
    dict(
        node_id=0,
        label="sat-a",
        first=1.4,
        interval=8.6,
        jitter=1.6,
        link_kbps=26.0,
        scenes=["great_lakes", "iowa", "tokyo", "sahara", "atacama"],
        cloudy=6,
    ),
    dict(
        node_id=1,
        label="sat-b",
        first=2.1,
        interval=8.0,
        jitter=1.2,
        link_kbps=30.0,
        scenes=["cairo", "amazon", "hawaii", "ganges"],
        cloudy=2,
    ),
    dict(
        node_id=2,
        label="sat-c",
        first=2.9,
        interval=9.0,
        jitter=1.8,
        link_kbps=21.0,
        scenes=["greenland", "alps", "netherlands", "himalaya", "pacific"],
        cloudy=11,
    ),
]
OUTAGE: dict[str, Any] = dict(node_id=2, start=47.5, length=13.4)
CRC_WARN: dict[str, Any] = dict(node_id=0, t=71.3, message="3 crc errors in last 10s")


def load_frames() -> dict[int, Frame]:
    """Every corpus frame scored against its scene reference (the golden model)."""
    from orbit import corpus as C
    from orbit.golden.score import score_frame

    c = C.load()
    out = {}
    for i in c.ids:
        i = int(i)
        ref = c.reference_for(i)
        s = score_frame(c.by_id(i), c.by_id(ref))
        out[i] = dict(
            frame_id=i,
            scene=c.scene_of(i),
            is_ref=(i == ref),
            score=s.score,
            clear=s.clear,
            sharp=s.sharp,
            change=s.change,
            cloud_frac=round(s.cloud_px / 16384, 4),
        )
    return out


def parts_of(f: Frame) -> dict[str, int]:
    """Weighted contributions that sum exactly to the score, as the contract example has them."""
    clear = (SCORING["w_clear"] * f["clear"]) >> 16
    sharp = (SCORING["w_sharp"] * f["sharp"]) >> 16
    return {"clear": clear, "sharp": sharp, "change": f["score"] - clear - sharp}


class Node:
    def __init__(self, plan: dict[str, Any], frames: list[Frame], rng: random.Random) -> None:
        self.id = plan["node_id"]
        self.label = plan["label"]
        self.plan = plan
        self.frames = frames  # capture order
        self.next_i = 0
        self.queue: list[tuple[int, int, float]] = []  # (score, frame_id, t_scored), kept head-first
        self.n_scored = self.n_evicted = self.n_sent = 0
        self.busy = False
        self.link = True
        self.buffered: list[Event] = []  # frame_scored payloads held while the link is down
        # (queue copy, n_scored, n_evicted, n_sent) frozen when the link dropped
        self.ground_view: tuple[list[tuple[int, int, float]], int, int, int] | None = (
            None  # (queue copy, n_scored, n_evicted, n_sent) frozen when the link dropped
        )
        self.rng = rng

    def insert(self, f: Frame, t: float) -> tuple[bool, int | None]:
        """protocol.md §5.3: returns (queued, evicted_frame_id)."""
        score, fid = f["score"], f["frame_id"]
        if len(self.queue) < QUEUE_LIMIT:
            evicted = None
        elif score > self.queue[-1][0]:
            evicted = self.queue.pop()[1]
            self.n_evicted += 1
        else:
            self.n_evicted += 1
            return False, fid
        pos = len(self.queue)
        for k, (s, _, _) in enumerate(self.queue):
            if s < score:
                pos = k
                break
        self.queue.insert(pos, (score, fid, t))
        return True, evicted

    def head(self) -> tuple[int, int, float] | None:
        return self.queue[0] if self.queue else None

    def window(self, t: float) -> list[dict[str, Any]]:
        return [{"frame_id": fid, "score": s, "age_s": round(t - ts, 1)} for s, fid, ts in self.queue[:5]]


class Sim:
    def __init__(self, seed: int, frames_by_id: dict[int, Frame], run_id: str = RUN_ID) -> None:
        self.run_id = run_id
        self.rng = random.Random(seed)
        self.events: list[Event] = []
        self.seq = 0
        self.last_t = -1.0
        self.heap: list[tuple[float, int, Callable[..., Any], tuple[Any, ...]]] = []
        self.counter = 0
        self.nodes: list[Node] = []
        by_scene: dict[str, list[Frame]] = {}
        for f in frames_by_id.values():
            if not f["is_ref"]:
                by_scene.setdefault(f["scene"], []).append(f)
        for plan in NODE_PLAN:
            rng = random.Random(seed * 100 + plan["node_id"])
            frames = [f for sc in plan["scenes"] for f in sorted(by_scene[sc], key=lambda x: x["frame_id"])]
            cloudy = [f for f in frames if f["cloud_frac"] > USABLE_MAX]
            clear = [f for f in frames if f["cloud_frac"] <= USABLE_MAX]
            rng.shuffle(cloudy)
            rng.shuffle(clear)
            pool = cloudy[: plan["cloudy"]] + clear[: PRELOAD - plan["cloudy"]]  # what this satellite "flies over"
            rng.shuffle(pool)  # capture order
            self.nodes.append(Node(plan, pool, rng))
        self.by_id = {n.id: n for n in self.nodes}
        self.frames_by_id = frames_by_id
        # ground state
        self.used = 0
        self.slot = 0
        self.open_grant: dict[str, Any] | None = None  # dict(node, slot, frame, t)
        self.next_grant_ok = 0.0
        self.winners: list[int] = []
        self.arrivals: list[Event] = []
        self.closed = False
        # baseline (FIFO, round robin, same limit)
        self.fifo: dict[int, list[Frame]] = {n.id: [] for n in self.nodes}
        self.fifo_dropped = 0
        self.rr = 0
        self.base: list[dict[str, Any]] = []
        self.outage_done = False

    # -- plumbing
    def emit(self, t: float, typ: str, **fields: Any) -> Event:
        t = round(max(t, self.last_t + 0.001), 3)
        self.last_t = t
        self.seq += 1
        ev = {"seq": self.seq, "t": t, "type": typ}
        ev.update(fields)
        ev["wall"] = (WALL0 + dt.timedelta(seconds=t)).strftime("%Y-%m-%dT%H:%M:%S.") + f"{round(t * 1000) % 1000:03d}Z"
        self.events.append(ev)
        return ev

    def at(self, t: float, fn: Callable[..., Any], *args: Any) -> None:
        self.counter += 1
        heapq.heappush(self.heap, (t, self.counter, fn, args))

    def run(self) -> list[Event]:
        self.at(0.0, self.start)
        while self.heap:
            t, _, fn, args = heapq.heappop(self.heap)
            if t > DURATION:
                break
            fn(t, *args)
        self.emit(DURATION, "run_end", **self.totals())
        return self.events

    # -- schedule
    def start(self, t: float) -> None:
        self.emit(
            0.0,
            "run_start",
            run_id=self.run_id,
            mode="live",
            # real=False, always: these satellites are the loop below, not boards.
            nodes=[{"node_id": n.id, "label": n.label, "transport": TRANSPORT, "real": False} for n in self.nodes],
            window={"budget_bytes": BUDGET, "duration_s": DURATION},
            queue_limit=QUEUE_LIMIT,
            scoring=SCORING,
            usable_rule={"metric": "cloud_frac", "max": USABLE_MAX},
        )
        for k, n in enumerate(self.nodes):
            self.emit(
                0.2 + 0.15 * k,
                "node_event",
                node_id=n.id,
                level="info",
                message=f"online: fw 0.4.2, {len(n.frames)} frames preloaded, reference set loaded",
            )
        self.at(0.6, self.poll)
        self.window_update(0.7)
        for n in self.nodes:
            self.at(n.plan["first"], self.score_next, n.id)
        self.at(1.0, self.arbitrate)
        self.at(OUTAGE["start"], self.outage_begin)
        self.at(
            CRC_WARN["t"],
            lambda t: self.emit(
                t, "node_event", node_id=CRC_WARN["node_id"], level="warn", message=CRC_WARN["message"]
            ),
        )

    def poll(self, t: float) -> None:
        for k, n in enumerate(self.nodes):
            if n.link:
                self.status(t + 0.03 * k, n)
            if t < 0.7:
                self.emit(t + 0.03 * k + 0.01, "queue_window", node_id=n.id, depth=0, top=[])
        nxt = t + POLL_S + self.rng.uniform(-0.02, 0.02)
        if nxt < DURATION - 0.05:
            self.at(nxt, self.poll)

    def status(self, t: float, n: Node, link_ok: bool = True) -> None:
        frozen = n.ground_view
        if link_ok or frozen is None:
            queue, n_scored, n_evicted, n_sent = n.queue, n.n_scored, n.n_evicted, n.n_sent
        else:
            queue, n_scored, n_evicted, n_sent = frozen
        h = queue[0] if queue else None
        self.emit(
            t,
            "node_status",
            node_id=n.id,
            queue_depth=len(queue),
            top_frame_id=h[1] if h else None,
            top_score=h[0] if h else 0,
            top_age_s=round(t - h[2], 1) if h else 0.0,
            frames_scored=n_scored,
            frames_evicted=n_evicted,
            frames_sent=n_sent,
            busy=n.busy,
            link_ok=link_ok,
        )

    def queue_window(self, t: float, n: Node) -> None:
        self.emit(t, "queue_window", node_id=n.id, depth=len(n.queue), top=n.window(t))

    def score_next(self, t: float, nid: int) -> None:
        n = self.by_id[nid]
        if n.next_i >= len(n.frames):
            return
        f = n.frames[n.next_i]
        n.next_i += 1
        queued, evicted = n.insert(f, t)
        n.n_scored += 1
        payload = dict(
            node_id=n.id,
            frame_id=f["frame_id"],
            score=f["score"],
            parts=parts_of(f),
            cloud_frac=f["cloud_frac"],
            queued=queued,
            evicted_frame_id=evicted,
            queue_depth=len(n.queue),
        )
        if n.link:
            self.report_scored(t, n, payload)
            self.queue_window(t + 0.02, n)
        else:
            n.buffered.append(payload)
        nxt = t + n.plan["interval"] + n.rng.uniform(-n.plan["jitter"], n.plan["jitter"])
        if nxt < DURATION - 1.0:
            self.at(nxt, self.score_next, nid)

    def report_scored(self, t: float, n: Node, payload: Event) -> None:
        self.emit(t, "frame_scored", **payload)
        f = self.frames_by_id[payload["frame_id"]]
        if len(self.fifo[n.id]) < QUEUE_LIMIT:  # the FIFO baseline stores captures in order, drops when full
            self.fifo[n.id].append(f)
        else:
            self.fifo_dropped += 1

    def window_update(self, t: float) -> None:
        remaining = BUDGET - self.used
        open_ = remaining >= FRAME_BYTES and t < DURATION
        if not open_:
            self.closed = True
        self.emit(
            t,
            "window_update",
            budget_bytes=BUDGET,
            used_bytes=self.used,
            remaining_bytes=remaining,
            time_remaining_s=round(DURATION - t, 1),
            open=open_,
        )

    def arbitrate(self, t: float) -> None:
        if not self.closed and self.open_grant is None and t >= self.next_grant_ok:
            bids = [
                {
                    "node_id": n.id,
                    "top_score": n.queue[0][0] if (n.link and n.queue) else 0,
                    "ready": bool(n.link and n.queue),
                }
                for n in self.nodes
            ]
            ready = sorted((b for b in bids if b["ready"]), key=lambda b: (-b["top_score"], b["node_id"]))
            if ready:
                best = ready[0]
                if len(ready) == 1:
                    winner, reason = best, "only_ready"
                elif len(self.winners) >= STARVATION_N and all(
                    w == best["node_id"] for w in self.winners[-STARVATION_N:]
                ):
                    winner, reason = ready[1], "starvation_forced"
                else:
                    winner, reason = best, "highest_score"
                self.grant(t, self.by_id[winner["node_id"]], reason, bids)
        if t + ARB_TICK_S < DURATION:
            self.at(t + ARB_TICK_S, self.arbitrate)

    def grant(self, t: float, n: Node, reason: str, bids: list[dict[str, Any]]) -> None:
        self.slot += 1
        score, fid, _ = n.queue.pop(0)  # the node pops its head on GRANT and starts sending
        n.busy = True
        self.winners.append(n.id)
        self.emit(
            t,
            "grant",
            slot_id=self.slot,
            node_id=n.id,
            budget_bytes=min(FRAME_BYTES, BUDGET - self.used),
            reason=reason,
            bids=bids,
        )
        self.queue_window(t + 0.05, n)
        duration = round(FRAME_BYTES * 8 / (n.plan["link_kbps"] * 1000) * self.rng.uniform(0.92, 1.12), 2)
        self.open_grant = dict(node=n, slot=self.slot, frame_id=fid, score=score, t=t)
        self.at(t + 0.25 + duration, self.arrive, duration)

    def arrive(self, t: float, duration: float) -> None:
        g = self.open_grant
        assert g is not None, "arrive() is only ever scheduled by grant()"
        n: Node = g["node"]
        f = self.frames_by_id[g["frame_id"]]
        n.busy = False
        n.n_sent += 1
        self.used += FRAME_BYTES
        self.open_grant = None
        ev = self.emit(
            t,
            "frame_arrived",
            slot_id=g["slot"],
            node_id=n.id,
            frame_id=f["frame_id"],
            score=f["score"],
            bytes=FRAME_BYTES,
            duration_s=duration,
            cloud_frac=f["cloud_frac"],
            usable=f["cloud_frac"] <= USABLE_MAX,
        )
        self.arrivals.append(ev)
        self.window_update(t + 0.01)
        self.baseline(t + 0.02)
        self.next_grant_ok = t + self.rng.uniform(0.9, 1.6)

    def baseline(self, t: float) -> None:
        for k in range(len(self.nodes)):
            nid = self.nodes[(self.rr + k) % len(self.nodes)].id
            if self.fifo[nid]:
                f = self.fifo[nid].pop(0)
                self.rr = (self.rr + k + 1) % len(self.nodes)
                ev = self.emit(
                    t,
                    "baseline_arrival",
                    node_id=nid,
                    frame_id=f["frame_id"],
                    score=f["score"],
                    bytes=FRAME_BYTES,
                    cloud_frac=f["cloud_frac"],
                    usable=f["cloud_frac"] <= USABLE_MAX,
                )
                self.base.append(ev)
                return

    def outage_begin(self, t: float) -> None:
        n = self.by_id[OUTAGE["node_id"]]
        if n.busy:  # never yank the link mid-transmission; try again shortly
            self.at(t + 0.5, self.outage_begin)
            return
        n.link = False
        n.ground_view = (list(n.queue), n.n_scored, n.n_evicted, n.n_sent)
        self.at(
            t + 2.0,
            lambda t2: self.emit(
                t2, "node_event", node_id=n.id, level="warn", message="heartbeat missed x2 (2.0 s), retrying"
            ),
        )

        def timeout(t3: float) -> None:
            self.emit(
                t3,
                "node_event",
                node_id=n.id,
                level="error",
                message=f"link down: no heartbeat for {LINK_TIMEOUT_S + 0.1:.1f} s, excluded from arbitration",
            )
            self.status(t3 + 0.01, n, link_ok=False)

        self.at(t + LINK_TIMEOUT_S + 0.1, timeout)
        self.at(t + OUTAGE["length"], self.outage_end)

    def outage_end(self, t: float) -> None:
        n = self.by_id[OUTAGE["node_id"]]
        n.link = True
        k = len(n.buffered)
        self.emit(
            t,
            "node_event",
            node_id=n.id,
            level="info",
            message=(
                f"link restored after {OUTAGE['length']:.1f} s,"
                f" {k} buffered frame report{'s' if k != 1 else ''} received"
            ),
        )
        for i, payload in enumerate(n.buffered):
            self.report_scored(t + 0.05 * (i + 1), n, payload)
        n.buffered = []
        self.queue_window(t + 0.05 * (k + 1), n)
        self.status(t + 0.05 * (k + 2), n)

    def totals(self) -> dict[str, Any]:
        usable = sum(1 for a in self.arrivals if a["usable"])
        b_usable = sum(1 for a in self.base if a["usable"])
        orbit = dict(
            frames_down=len(self.arrivals),
            usable_down=usable,
            bytes_used=self.used,
            frames_left_queued=sum(len(n.queue) for n in self.nodes),
        )
        base = dict(
            frames_down=len(self.base),
            usable_down=b_usable,
            bytes_used=len(self.base) * FRAME_BYTES,
            frames_left_queued=sum(len(q) for q in self.fifo.values()),
        )
        gain = round(usable / b_usable, 2) if b_usable else None
        return dict(
            orbit=orbit,
            baseline=base,
            headline={"metric": "usable frames downlinked", "orbit": usable, "baseline": b_usable, "gain": gain},
        )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("-o", "--out", default=str(REPO / "runs" / "sample.jsonl"))
    ap.add_argument(
        "--seed",
        type=int,
        default=8,
        help="8 is runs/sample.jsonl; 39 is the committed demo, and also has frames rejected by a full queue",
    )
    ap.add_argument("--run-id", default=RUN_ID, help="run_id in run_start; keep a committed file's id when redoing it")
    ap.add_argument("--debug", action="store_true", help="print the frame pools and the slot sequence")
    args = ap.parse_args(argv)
    sim = Sim(args.seed, load_frames(), args.run_id)
    events = sim.run()
    if args.debug:
        for n in sim.nodes:
            print(
                f"{n.label} pool: "
                + " ".join(
                    f"#{f['frame_id']}:{f['score'] * 100 / 65535:.0f}{'c' if f['cloud_frac'] > USABLE_MAX else ''}"
                    for f in n.frames
                )
            )
        for a in sim.arrivals:
            g = next(e for e in events if e["type"] == "grant" and e["slot_id"] == a["slot_id"])
            bids = " ".join(f"{b['top_score'] * 100 / 65535:5.1f}{'*' if b['ready'] else ' '}" for b in g["bids"])
            print(
                f"slot {a['slot_id']:2d} t={g['t']:6.1f} -> {sim.by_id[a['node_id']].label} #{a['frame_id']:<3d} "
                f"{a['score'] * 100 / 65535:5.1f} {'usable' if a['usable'] else 'CLOUDY'}"
                f"  {g['reason']:17s} bids {bids}"
            )
        print(
            "baseline: "
            + " ".join(
                f"{sim.by_id[b['node_id']].label[-1]}#{b['frame_id']}{'' if b['usable'] else 'c'}" for b in sim.base
            )
        )
    rep = check_run.check_events(events)
    print(check_run.format_summary(rep.summary, args.out))
    for seq, msg in rep.errors[:20]:
        print(f"E seq {seq}  {msg}")
    for seq, msg in rep.warnings[:20]:
        print(f"W seq {seq}  {msg}")
    if not rep.ok:
        print(f"NOT WRITTEN: {len(rep.errors)} errors")
        return 1
    Path(args.out).parent.mkdir(parents=True, exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        for ev in events:
            f.write(json.dumps(ev, separators=(",", ":")) + "\n")
    print(f"wrote {args.out}: {len(events)} events, {len(rep.warnings)} warnings")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
