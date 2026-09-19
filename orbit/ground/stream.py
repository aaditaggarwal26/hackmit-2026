"""The display-facing event stream (docs/event_stream.md).

The bus protocol is the satellites' business; the display never sees it. What the display
sees is this stream: one JSON object per line, ``seq`` contiguous from 1, ``t`` seconds
since run start, delivered over a WebSocket the ground hosts and appended, line for line,
to ``runs/<run_id>.jsonl``. Live and replay are the same bytes.

``EventStream`` is a pure translator: the ground station (live or simulated) feeds it the
internal events it already produces plus the bus traffic it already sees, and it emits the
contract's event types. It also runs the thing the pitch is measured on — the **FIFO
baseline**: the same frames, the same byte budget, no scoring, round-robin between nodes,
newest frame dropped when a node's pool is full. ``usable`` is decided from cloud fraction
alone, never from the score that did the ranking; ranking by a number and then measuring
that number would be circular.

Nothing here may slow arbitration: emitting is a list append and a few callbacks; the
WebSocket side uses bounded per-client queues and drops a client that falls behind.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from orbit import config
from orbit.config import Settings
from orbit.log import log
from orbit.protocol import messages as M

lg = logging.getLogger("orbit.stream")

Writer = Callable[[str], None]
QUEUE_WINDOW_CAP = 5  # the contract caps queue_window.top at 5 entries


def new_run_id() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H-%M-%S")


@dataclass
class NodeView:
    """What the stream last heard about one satellite, for node_status/run_end."""

    node_id: int
    label: str
    real: bool
    last_seen: float = -1e9
    queue_depth: int = 0
    top_frame_id: int = -1
    top_score: float = 0.0
    top_age_s: float = 0.0
    frames_scored: int = 0
    frames_evicted: int = 0
    frames_sent: int = 0
    busy: bool = False
    hard: bool = False
    silent: bool = False


@dataclass
class FrameMeta:
    node_id: int
    frame_id: int
    score: float
    cloud_frac: float


@dataclass
class FifoBaseline:
    """No scoring, no prioritisation: each node keeps arrivals in order in a pool of the same size,
    dropping the newest when full; the ground serves nodes round-robin. Same byte budget as Orbit."""

    frame_bytes: int
    queues: dict[int, deque[FrameMeta]] = field(default_factory=dict)
    caps: dict[int, int] = field(default_factory=dict)
    order: list[int] = field(default_factory=list)
    rr: int = 0
    dropped: int = 0
    frames_down: int = 0
    usable_down: int = 0
    bytes_used: int = 0

    def scored(self, meta: FrameMeta, cap: int) -> None:
        q = self.queues.setdefault(meta.node_id, deque())
        if meta.node_id not in self.caps:
            self.order.append(meta.node_id)
        self.caps[meta.node_id] = cap
        if len(q) >= cap:
            self.dropped += 1  # a FIFO has no opinion: the newest is the one that does not fit
            return
        q.append(meta)

    def take(self, remaining_bytes: int) -> FrameMeta | None:
        if remaining_bytes < self.frame_bytes or not self.order:
            return None
        for _ in range(len(self.order)):
            node = self.order[self.rr % len(self.order)]
            self.rr += 1
            q = self.queues[node]
            if q:
                return q.popleft()
        return None

    @property
    def left_queued(self) -> int:
        return sum(len(q) for q in self.queues.values())


class EventStream:
    def __init__(self, settings: Settings, run_id: str, *, mode: str = "live", writers: list[Writer] | None = None,
                 wall: bool = True) -> None:
        self.s = settings
        self.run_id = run_id
        self.mode = mode
        self.writers: list[Writer] = list(writers or [])
        self.wall = wall
        self.lines: list[str] = []  # everything emitted, for late-connecting displays
        self.seq = 0
        self.nodes: dict[str, NodeView] = {}
        for i, name in enumerate(n for n in settings.expected_sats.split(",") if n):
            self.nodes[name] = NodeView(node_id=i, label=name, real=settings.nodes_real)
        self.frames: dict[tuple[int, int], FrameMeta] = {}
        self.baseline = FifoBaseline(frame_bytes=settings.frame_bytes)
        self.baseline_budget_used = 0
        self.orbit_frames_down = 0
        self.orbit_usable_down = 0
        self.orbit_bytes_used = 0
        self.ended = False
        self.started = False

    # ------------------------------------------------------------------ lifecycle

    def start(self, now: float) -> None:
        if self.started:
            return
        self.started = True
        self._emit("run_start", now, run_id=self.run_id, mode=self.mode,
                   nodes=[dict(node_id=v.node_id, label=v.label,
                               transport=f"udp-multicast://{self.s.mcast_group}:{self.s.mcast_port}", real=v.real)
                          for v in sorted(self.nodes.values(), key=lambda v: v.node_id)],
                   window=dict(budget_bytes=self.s.window_capacity_bytes, duration_s=self.s.window_duration_s),
                   queue_limit=self.s.sat_buffer_slots,
                   scoring=dict(w_clear=config.W_CLEAR, w_sharp=config.W_SHARP, w_change=config.W_CHANGE,
                                cloud_thr=config.CLOUD_THRESHOLD, change_thr=config.CHANGE_THRESHOLD),
                   usable_rule=dict(metric="cloud_frac", max=self.s.usable_cloud_max),
                   priority=dict(item_aging_rate=self.s.item_aging_rate, sat_aging_rate=self.s.sat_aging_rate),
                   bus=dict(group=self.s.mcast_group, port=self.s.mcast_port))

    def end(self, now: float, reason: str = "window_closed") -> None:
        if self.ended:
            return
        self.ended = True
        orbit = dict(frames_down=self.orbit_frames_down, usable_down=self.orbit_usable_down,
                     bytes_used=self.orbit_bytes_used,
                     frames_left_queued=sum(v.queue_depth for v in self.nodes.values()))
        b = self.baseline
        base = dict(frames_down=b.frames_down, usable_down=b.usable_down, bytes_used=b.bytes_used,
                    frames_left_queued=b.left_queued, frames_dropped_full=b.dropped)
        gain = (self.orbit_usable_down / b.usable_down) if b.usable_down else None
        self._emit("run_end", now, orbit=orbit, baseline=base, reason=reason,
                   headline=dict(metric="usable frames downlinked", orbit=self.orbit_usable_down,
                                 baseline=b.usable_down, gain=round(gain, 3) if gain is not None else None))

    # ------------------------------------------------------------------ inputs

    def on_bus(self, msg: M.Message, now: float, outbound: bool) -> None:
        """Bus traffic as the ground saw it. Only satellite messages carry display-relevant state."""
        if outbound or msg.TYPE in M.GROUND_TYPES:
            return
        v = self._node(msg.sender, now)
        v.last_seen = now
        match msg:
            case M.Scored():
                self._on_scored(msg, v, now)
            case M.Bid():
                v.queue_depth, v.top_frame_id = msg.queue_len, msg.item_id
                v.top_score, v.top_age_s = msg.score, msg.item_age_s
                v.frames_evicted = msg.eviction_count
                self._node_status(v, now)
                top = [dict(frame_id=msg.item_id, score=msg.score, age_s=msg.item_age_s)]
                top += [dict(frame_id=e.item_id, score=e.score, age_s=e.item_age_s) for e in msg.window]
                self._emit("queue_window", now, node_id=v.node_id, label=v.label, depth=msg.queue_len,
                           top=top[:QUEUE_WINDOW_CAP])
            case M.Heartbeat():
                v.queue_depth, v.top_frame_id, v.top_score = msg.queue_len, msg.top_item_id, msg.top_score
                v.frames_evicted = msg.eviction_count
                v.frames_scored, v.frames_sent = msg.frames_scored, msg.frames_sent
                self._node_status(v, now)

    def on_event(self, kind: str, p: dict[str, Any]) -> None:
        """The ground's internal events (GroundStation._emit)."""
        now = float(p.get("t", 0.0))
        match kind:
            case "sat_seen":
                v = self._node(str(p["sat"]), now)
                self._node_event(v, now, "info", f"{v.label} joined the bus as node {v.node_id}")
            case "decision":
                self._on_decision(p, now)
            case "complete":
                self._on_complete(p, now)
            case "tx_failed":
                v = self._node(str(p["sat"]), now)
                v.busy = False
                self._node_event(v, now, "warn", f"transmission of #{p['item_id']} failed: {p['reason']}; frame kept")
            case "revoke":
                v = self._node(str(p["sat"]), now)
                v.busy = False
                self._node_event(v, now, "warn", f"grant for #{p['item_id']} revoked: {p['reason']}")
            case "eviction":
                v = self._node(str(p["sat"]), now)
                what = "evicted" if p.get("loss_kind") == "evicted" else "rejected"
                by = ""
                if p.get("displaced_by", -1) != -1:
                    by = f" (displaced by #{p['displaced_by']} scoring {p['displaced_by_score']:.1f})"
                self._node_event(v, now, "info",
                                 f"{what} #{p['item_id']} scoring {p['score']:.1f}{by}: onboard storage full")
            case "no_bids":
                excl = f" (excluded {p['excluded']})" if p.get("excluded") else ""
                self._emit("node_event", now, node_id=None, level="info",
                           message=f"round {p['round_id']}: no bids{excl}")
            case "late_bid":
                v = self._node(str(p["sat"]), now)
                self._node_event(v, now, "info", f"bid for round {p['bid_round']} arrived during round {p['round_id']}")
            case "unexpected_tx":
                v = self._node(str(p["sat"]), now)
                self._node_event(v, now, "warn", f"unexpected {p['type']} for #{p['item_id']} (round {p['round_id']})")
            case "flags":
                for host, f in dict(p.get("sats", {})).items():
                    self._on_flags(str(host), dict(f), now)
            case "window_closed":
                self._window_update(dict(p["window"]), now)
                self.end(now, "window_closed")

    # ------------------------------------------------------------------ translation

    def _on_scored(self, msg: M.Scored, v: NodeView, now: float) -> None:
        v.frames_scored += 1  # the next heartbeat re-syncs this to the satellite's own counter
        meta = FrameMeta(node_id=v.node_id, frame_id=msg.item_id, score=msg.score, cloud_frac=msg.cloud_frac)
        self.frames[(v.node_id, msg.item_id)] = meta
        self.baseline.scored(meta, self.s.sat_buffer_slots)
        self._emit("frame_scored", now, node_id=v.node_id, label=v.label, frame_id=msg.item_id, score=msg.score,
                   parts=dict(clear=msg.parts.clear, sharp=msg.parts.sharp, change=msg.parts.change),
                   cloud_frac=msg.cloud_frac, queued=msg.queued,
                   evicted_frame_id=msg.evicted_item_id if msg.evicted_item_id >= 0 else None,
                   queue_depth=msg.queue_depth)

    def _on_decision(self, p: dict[str, Any], now: float) -> None:
        ranked = list(p.get("ranked", []))
        bidders = {str(c["sat"]): c for c in ranked}
        winner = self._node(str(p["winner"]), now)
        winner.busy = True
        for h, v in self.nodes.items():
            if h != winner.label:
                v.busy = False
        top_raw = max((float(c["score"]) for c in ranked), default=0.0)
        if len(ranked) == 1:
            reason = "only_ready"
        elif float(bidders[winner.label]["score"]) < top_raw:
            reason = "starvation_forced"  # the aging terms, not the raw score, decided this slot
        else:
            reason = "highest_score"
        bids = []
        for h, v in sorted(self.nodes.items(), key=lambda kv: kv[1].node_id):
            c = bidders.get(h)
            if c is None:
                bids.append(dict(node_id=v.node_id, top_score=0, ready=False))
            else:
                bids.append(dict(node_id=v.node_id, top_score=c["score"], ready=True, priority=c["total"],
                                 item_age_term=c["item_age_term"], sat_wait_term=c["sat_wait_term"],
                                 item_age_s=c["item_age_s"], sat_wait_s=c["sat_wait_s"], frame_id=c["item_id"]))
        wb = bidders[winner.label]
        self._emit("grant", now, slot_id=int(p["round_id"]), node_id=winner.node_id, budget_bytes=self.s.frame_bytes,
                   reason=reason, bids=bids, frame_id=wb["item_id"],
                   priority=dict(score=wb["score"], item_age_term=wb["item_age_term"],
                                 sat_wait_term=wb["sat_wait_term"], total=wb["total"]), margin=p.get("margin"))

    def _on_complete(self, p: dict[str, Any], now: float) -> None:
        v = self._node(str(p["sat"]), now)
        v.busy = False
        v.frames_sent += 1
        v.queue_depth = max(0, v.queue_depth - 1)
        fid = int(p["item_id"])
        cloud = p.get("cloud_frac")
        meta = self.frames.get((v.node_id, fid))
        if (cloud is None or cloud < 0) and meta is not None:
            cloud = meta.cloud_frac
        usable = None if cloud is None else bool(float(cloud) <= self.s.usable_cloud_max)
        nbytes = int(p.get("bytes") or self.s.frame_bytes)
        self.orbit_frames_down += 1
        self.orbit_usable_down += 1 if usable else 0
        self.orbit_bytes_used += self.s.frame_bytes
        self._emit("frame_arrived", now, slot_id=int(p["round_id"]), node_id=v.node_id, frame_id=fid,
                   score=p.get("score"), bytes=nbytes, duration_s=round(now - float(p.get("granted_at", now)), 3),
                   cloud_frac=cloud, usable=usable)
        # the baseline gets the same slot: one frame of budget, FIFO, round-robin, no scoring
        w = dict(p["window"])
        remaining_for_baseline = int(w["capacity_bytes"]) - self.baseline.bytes_used
        b = self.baseline.take(remaining_for_baseline)
        if b is not None:
            b_usable = b.cloud_frac <= self.s.usable_cloud_max
            self.baseline.frames_down += 1
            self.baseline.usable_down += 1 if b_usable else 0
            self.baseline.bytes_used += self.s.frame_bytes
            self._emit("baseline_arrival", now, node_id=b.node_id, frame_id=b.frame_id, score=b.score,
                       bytes=self.s.frame_bytes, cloud_frac=b.cloud_frac, usable=b_usable)
        self._window_update(w, now)

    def _on_flags(self, host: str, f: dict[str, Any], now: float) -> None:
        v = self._node(host, now)
        hard = str(f.get("level")) == "hard"
        silent = bool(f.get("silent"))
        if hard and not v.hard:
            self._node_event(v, now, "warn", f"HARD flag: {f.get('reason', '')}")
        if silent and not v.silent:
            self._node_event(v, now, "error", f"silent: {f.get('reason', '')}")
        if v.silent and not silent:
            self._node_event(v, now, "info", "heard again")
        v.hard, v.silent = hard, silent

    def _window_update(self, w: dict[str, Any], now: float) -> None:
        self._emit("window_update", now, budget_bytes=int(w["capacity_bytes"]), used_bytes=int(w["used_bytes"]),
                   remaining_bytes=int(w["remaining_bytes"]),
                   time_remaining_s=round(max(0.0, self.s.window_duration_s - now), 1), open=bool(w["open"]),
                   slots_remaining=int(w.get("slots_remaining", 0)))

    def _node_status(self, v: NodeView, now: float) -> None:
        self._emit("node_status", now, node_id=v.node_id, label=v.label, queue_depth=v.queue_depth,
                   top_frame_id=v.top_frame_id if v.queue_depth else None,
                   top_score=v.top_score if v.queue_depth else 0,
                   top_age_s=v.top_age_s if v.queue_depth else 0.0, frames_scored=v.frames_scored,
                   frames_evicted=v.frames_evicted, frames_sent=v.frames_sent, busy=v.busy,
                   link_ok=(now - v.last_seen) <= self.s.peer_stale_s)

    def _node_event(self, v: NodeView, now: float, level: str, message: str) -> None:
        self._emit("node_event", now, node_id=v.node_id, label=v.label, level=level, message=message)

    def _node(self, host: str, now: float) -> NodeView:
        v = self.nodes.get(host)
        if v is None:
            v = self.nodes[host] = NodeView(node_id=len(self.nodes), label=host, real=self.s.nodes_real)
        return v

    def _emit(self, type_: str, now: float, **fields: Any) -> None:
        self.seq += 1
        doc: dict[str, Any] = {"seq": self.seq, "t": round(now, 3), "type": type_}
        if self.wall:
            doc["wall"] = datetime.now(UTC).isoformat(timespec="milliseconds")
        doc.update(fields)
        line = json.dumps(doc, separators=(",", ":"), default=_default)
        self.lines.append(line)
        for w in self.writers:
            try:
                w(line)
            except Exception:  # a writer failing must never cost the ground a decision
                lg.exception("stream writer failed")


def _default(o: Any) -> Any:
    if isinstance(o, set | frozenset | tuple):
        return list(o)
    return str(o)


# ------------------------------------------------------------------ sinks


class RunFile:
    """runs/<run_id>.jsonl, one line per event, flushed per line so a demo hiccup loses nothing."""

    def __init__(self, runs_dir: str, run_id: str) -> None:
        self.path = Path(runs_dir) / f"{run_id}.jsonl"
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = self.path.open("a", encoding="utf-8")

    def __call__(self, line: str) -> None:
        self._f.write(line + "\n")
        self._f.flush()

    def close(self) -> None:
        self._f.close()


class StreamServer:
    """WebSocket fan-out. A client gets the whole run so far on connect, then live lines.
    Each client has a bounded queue; one that cannot keep up is closed, not waited for."""

    def __init__(self, stream: EventStream, host: str, port: int, queue_max: int) -> None:
        self.stream = stream
        self.host, self.port, self.queue_max = host, port, queue_max
        self._clients: set[asyncio.Queue[str | None]] = set()
        self._server: Any = None
        stream.writers.append(self.publish)

    def publish(self, line: str) -> None:
        for q in list(self._clients):
            try:
                q.put_nowait(line)
            except asyncio.QueueFull:
                self._clients.discard(q)
                with contextlib.suppress(asyncio.QueueFull):
                    q.put_nowait(None)  # tells the handler to close
                log(lg, logging.WARNING, "stream_client_dropped", queued=q.qsize())

    async def _handler(self, ws: Any) -> None:
        q: asyncio.Queue[str | None] = asyncio.Queue(maxsize=self.queue_max)
        backlog = list(self.stream.lines)  # snapshot before subscribing so nothing is skipped or doubled
        self._clients.add(q)
        try:
            for line in backlog:
                await ws.send(line)
            while True:
                item = await q.get()
                if item is None:
                    break
                await ws.send(item)
        except Exception:  # client went away
            pass
        finally:
            self._clients.discard(q)

    async def start(self) -> None:
        from websockets.asyncio.server import serve
        self._server = await serve(self._handler, self.host, self.port)
        log(lg, logging.INFO, "stream_listening", host=self.host, port=self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            with contextlib.suppress(Exception):
                await asyncio.wait_for(self._server.wait_closed(), 2.0)
            self._server = None

    @property
    def clients(self) -> int:
        return len(self._clients)


def wall_now() -> float:
    return time.time()
