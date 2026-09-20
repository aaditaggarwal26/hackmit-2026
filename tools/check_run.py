#!/usr/bin/env python3
"""Check a run file against docs/event_stream.md.

    python3 tools/check_run.py runs/sample.jsonl          # errors, warnings, summary; exit 1 on errors
    python3 tools/check_run.py runs/sample.jsonl --quiet  # one summary block, no per-event lines
    cat some.jsonl | python3 tools/check_run.py -

Display-side tool, stdlib only. Point it at any runs/<run_id>.jsonl, ours or the
ground station's, and it verifies what the live screen relies on:

  envelope   seq starts at 1 and never skips, t never goes backwards, run_start
             first and once, nothing after run_end
  shapes     every event type carries its fields with the right types
             (extra fields are fine, per the contract)
  frames     every frame_arrived was frame_scored earlier by that node, was
             queued, and had not already been sent or evicted; score matches
  queues     queue_depth in frame_scored / node_status / queue_window equals the
             queue rebuilt from the stream; depth never exceeds queue_limit;
             queue_window entries are real, sorted, and match node_status
  grants     the winner is a ready bidder; reason agrees with the bids; one
             frame_arrived per grant, same slot and node. A revoked or failed
             grant delivers nothing, keeps the frame with the satellite, and
             re-arbitrates the same slot_id without that node
  nodes      a satellite the roster never mentioned may still turn up, with the
             next node_id and a node_event announcing it
  window     used/remaining bytes equal the sum of arrivals against the budget
  usable     follows cloud_frac and usable_rule only, never score
  run_end    totals equal what the events said

Errors are contract violations. Warnings are places the contract leaves open
(what `busy` means, what frames_evicted counts, ...) where the file differs
from the reading the display uses. They deserve a message in the group chat,
not a failed check.

Counts a node reports about itself (node_status, queue_window, frame_scored)
are its own, and the frame it just sent leaves it at the ground's tx_ack, not
at frame_arrived. Exactly one report per delivery may still count that frame;
the next one must agree with the ground.
"""

from __future__ import annotations

import json
import re
import sys
from collections import Counter, OrderedDict
from collections.abc import Iterable
from typing import Any

Event = dict[str, Any]

TYPES = (
    "run_start",
    "node_status",
    "queue_window",
    "frame_scored",
    "grant",
    "frame_arrived",
    "baseline_arrival",
    "window_update",
    "node_event",
    "run_end",
    "ground_status",
    "bus_health",
)
REASONS = ("highest_score", "starvation_forced", "only_ready")
LEVELS = ("info", "warn", "error")
MODES = ("live", "replay")
EMPTY_TOP_ID = (None, 0xFFFF)  # top_frame_id of an empty queue: null, or 0xFFFF as the wire protocol has it
WINDOW_CAP = 5  # queue_window: "Cap at 5 entries"

# node_event messages that say the open grant ended with nothing delivered and the round was
# re-arbitrated on the same slot_id: a revoke ("grant for #14 revoked: grant_timeout") or a failed
# transmission ("transmission of #14 failed: <reason>; frame kept"). The frame is kept by the
# satellite either way. This message text is the only signal the contract gives the display.
GRANT_ENDED = re.compile(r"grant for #\d+ revoked\b|transmission of #\d+ failed\b")
JOINED = re.compile(r"\bjoined the bus\b")

Int, Num, Bool, Str, List, Dict = "int", "num", "bool", "str", "list", "dict"
NULLABLE = "?"
SCHEMA = {
    "run_start": {
        "run_id": Str,
        "mode": Str,
        "nodes": List,
        "window": Dict,
        "queue_limit": Int,
        "scoring": Dict,
        "usable_rule": Dict,
    },
    "node_status": {
        "node_id": Int,
        "queue_depth": Int,
        "top_frame_id": Int + NULLABLE,
        "top_score": Num,
        "top_age_s": Num,
        "frames_scored": Int,
        "frames_evicted": Int,
        "frames_sent": Int,
        "busy": Bool,
        "link_ok": Bool,
    },
    "queue_window": {"node_id": Int, "depth": Int, "top": List},
    "frame_scored": {
        "node_id": Int,
        "frame_id": Int,
        "score": Num,
        "parts": Dict,
        "cloud_frac": Num,
        "queued": Bool,
        "evicted_frame_id": Int + NULLABLE,
        "queue_depth": Int,
    },
    "grant": {"slot_id": Int, "node_id": Int, "budget_bytes": Int, "reason": Str, "bids": List},
    "frame_arrived": {
        "slot_id": Int,
        "node_id": Int,
        "frame_id": Int,
        "score": Num,
        "bytes": Int,
        "duration_s": Num,
        "cloud_frac": Num,
        "usable": Bool,
    },
    "baseline_arrival": {
        "node_id": Int,
        "frame_id": Int,
        "score": Num,
        "bytes": Int,
        "cloud_frac": Num,
        "usable": Bool,
    },
    "window_update": {
        "budget_bytes": Int,
        "used_bytes": Int,
        "remaining_bytes": Int,
        "time_remaining_s": Num,
        "open": Bool,
    },
    "node_event": {"node_id": Int + NULLABLE, "level": Str, "message": Str},
    "run_end": {"orbit": Dict, "baseline": Dict, "headline": Dict},
    "ground_status": {
        "uptime_s": Num,
        "state": Str,
        "rounds": Int,
        "period_s": Num,
        "nodes": Dict,
        "window": Dict,
    },
    "bus_health": {
        "window_s": Num,
        "measured_s": Num,
        "received": Int,
        "delivered": Int,
        "dropped": Dict,
        "rates_per_min": Dict,
        "alerts": List,
    },
}
TOTALS = ("frames_down", "usable_down", "bytes_used", "frames_left_queued")


def is_type(v: Any, spec: str) -> bool:
    if v is None:
        return spec.endswith(NULLABLE)
    spec = spec.rstrip(NULLABLE)
    if spec == Bool:
        return isinstance(v, bool)
    if isinstance(v, bool):  # bool is an int in Python; the contract's ints are not bools
        return False
    if spec == Int:
        return isinstance(v, int)
    if spec == Num:
        return isinstance(v, (int, float))
    return isinstance(v, {Str: str, List: list, Dict: dict}[spec])


def ranked_of(queue: OrderedDict[Any, float]) -> list[tuple[Any, float]]:
    order = {fid: i for i, fid in enumerate(queue)}
    return sorted(queue.items(), key=lambda kv: (-kv[1], order[kv[0]]))


def head_of(queue: OrderedDict[Any, float]) -> tuple[Any, float] | None:
    """(frame_id, score) of the head: highest score, earliest insert among equals."""
    return ranked_of(queue)[0] if queue else None


class NodeState:
    def __init__(self, label: str) -> None:
        self.label = label
        self.scored: dict[Any, dict[str, Any]] = {}  # frame_id -> {"t", "score", "cloud_frac"}
        self.queue: OrderedDict[Any, float] = OrderedDict()  # frame_id -> score, in insertion order
        self.n_scored = self.n_evicted = self.n_sent = self.n_rejected = 0
        self.last_status_t: float | None = None
        self.max_quiet = 0.0  # longest gap between node_status events, stream seconds
        self.link_ok: bool | None = None
        # (frame_id, score, position in the queue) of the frame this node was granted; None when idle
        self.in_flight: tuple[Any, float, int] | None = None
        # The satellite drops a frame when the ground's tx_ack reaches it, which is after the ground
        # emitted frame_arrived, so its own next report can still count the frame. This is that frame,
        # and ack_lag_t the stream time of the one report allowed to still show it.
        self.ack_pending: tuple[Any, float, int] | None = None
        self.ack_lag_t: float | None = None

    def head(self) -> tuple[Any, float] | None:
        return head_of(self.queue)

    def ranked(self) -> list[tuple[Any, float]]:
        return ranked_of(self.queue)

    def with_frame(self, held: tuple[Any, float, int]) -> OrderedDict[Any, float]:
        """The queue with `held` put back where it was: the satellite's own view of itself."""
        fid, score, pos = held
        q: OrderedDict[Any, float] = OrderedDict()
        for i, (k, v) in enumerate(self.queue.items()):
            if i == pos:
                q[fid] = score
            q[k] = v
        if fid not in q:
            q[fid] = score
        return q

    def sat_queue(self) -> OrderedDict[Any, float] | None:
        """The queue as the satellite still sees it while a delivery is waiting for its tx_ack."""
        return None if self.ack_pending is None else self.with_frame(self.ack_pending)


class Report:
    def __init__(self) -> None:
        self.errors: list[tuple[int | None, str]] = []
        self.warnings: list[tuple[int | None, str]] = []
        self.notes: list[tuple[int | None, str]] = []  # conventions the file uses where the contract is open
        self.summary: dict[str, Any] = {}

    @property
    def ok(self) -> bool:
        return not self.errors


class Checker:
    def __init__(self) -> None:
        self.r = Report()
        self.seq_expected = 1
        self.last_t: float | None = None
        self.run: Event | None = None
        self.ended = False
        self.nodes: dict[Any, NodeState] = {}
        self.queue_limit: int | None = None
        self.budget: int | None = None
        self.duration: float | None = None
        self.usable_max: float | None = None  # None when usable_rule is not on cloud_frac (then usable is not checked)
        self.frame_owner: dict[Any, Any] = {}
        self.open_grant: Event | None = None  # the grant event whose frame has not arrived yet
        self.grant_t: float | None = None
        self.pop_at: str | None = (
            None  # "grant" or "arrival": when the sent frame leaves the queue. Learned from the file.
        )
        self.last_slot: Any = None
        # slot_id -> the nodes whose grant on that slot was revoked or failed. The contract lets a
        # revoked slot re-use its slot_id with a new grant; the arbiter re-runs the same round's bids
        # without that node, so it must not win the slot again.
        self.revoked_from: dict[Any, set[Any]] = {}
        self.late_joiners: set[Any] = set()  # node_ids admitted without a run_start roster entry
        self.announced: set[Any] = set()  # node_ids a node_event said joined the bus
        self.ack_note_done = False
        self.winners: list[Any] = []
        self.n_arrived = self.n_usable = self.bytes_used = 0
        self.base_sent: set[Any] = set()
        self.base_n = self.base_usable = self.base_bytes = 0
        self.last_window: Event | None = None
        self.window_closed_t: float | None = None
        self.types: Counter[str] = Counter()
        self.reasons: Counter[str] = Counter()
        self.levels: Counter[str] = Counter()
        self.unknown_types: Counter[str] = Counter()
        self.run_end: Event | None = None
        self.n_lines = 0
        self.bad_lines = 0

    # -- reporting helpers
    def E(self, seq: int | None, msg: str) -> None:
        self.r.errors.append((seq, msg))

    def W(self, seq: int | None, msg: str) -> None:
        self.r.warnings.append((seq, msg))

    def N(self, seq: int | None, msg: str) -> None:
        self.r.notes.append((seq, msg))

    GROUND = NodeState("the ground")

    def node(self, seq: int | None, ev: Event, key: str = "node_id") -> NodeState:
        nid = ev.get(key)
        if nid is None:  # the ground's own event, not any satellite's
            return self.GROUND
        if nid in self.nodes:
            return self.nodes[nid]
        label = ev.get("label") if is_type(ev.get("label"), Str) else None
        if self.run is None:
            self.W(seq, f"{ev.get('type')}: node_id {nid!r} seen before run_start")
        elif nid == len(self.nodes):
            # event_stream.md, run_start: "a satellite that appears with another hostname gets the
            # next node_id and a node_event announcing it". Nothing has to precede it on the stream.
            self.late_joiners.add(nid)
            self.N(seq, f"{ev.get('type')}: node {nid} ({label or '?'}) joined late, after run_start")
        else:
            self.E(seq, f"{ev.get('type')}: node_id {nid!r} is neither in run_start.nodes nor the next node_id")
        st = self.nodes.setdefault(nid, NodeState(label or f"node {nid}"))
        return st

    def ack_lag(self, seq: int | None, st: NodeState, t: float | None, ground_ok: bool, sat_ok: bool) -> bool:
        """Is this the tx_ack lag? A satellite-sourced count can disagree with the ground for exactly
        one report after a delivery: frame_arrived fires on tx_done, while the satellite drops the
        frame and bumps frames_sent when the ground's tx_ack gets back to it (protocol/messages.py:
        Heartbeat.frames_sent is "since boot, confirmed by tx_ack"). Returns True when the report
        matches the satellite's pre-ack view and is the first one since the delivery."""
        if st.ack_pending is None or ground_ok:
            st.ack_pending = st.ack_lag_t = None  # caught up: strict from here
            return False
        if not sat_ok or (st.ack_lag_t is not None and t != st.ack_lag_t):
            st.ack_pending = st.ack_lag_t = None  # a second stale report is not the ack lag
            return False
        if st.ack_lag_t is None:
            st.ack_lag_t = t
            if not self.ack_note_done:
                self.ack_note_done = True
                self.N(seq, f"{st.label} reported its queue once more before the tx_ack landed (one report behind)")
        return True

    def resolve_depth(self, seq: int | None, st: NodeState, observed: int, what: str) -> None:
        """A depth observation while a grant is open decides when the sent frame leaves the
        queue (event_stream.md leaves it open): at the grant, or when frame_arrived comes. The first
        observation locks the convention for the whole file; a mismatch after that is an error."""
        if st.in_flight is None or st.in_flight[0] not in st.queue:
            return
        if self.pop_at is None:
            if observed == len(st.queue) - 1:
                self.pop_at = "grant"
            elif observed == len(st.queue):
                self.pop_at = "arrival"
            else:
                return  # let the caller report the mismatch
            self.N(
                seq,
                f"the transmitted frame leaves the queue at the "
                f"{'grant' if self.pop_at == 'grant' else 'frame_arrived'} (first seen in {what} for {st.label})",
            )
        if self.pop_at == "grant":
            del st.queue[st.in_flight[0]]

    def end_grant(self, st: NodeState) -> None:
        """A grant ended with nothing delivered (revoke or failed transmission). The satellite keeps
        the frame — `orbit/arbiter/fsm.py` re-runs the same round's bids without that node — so the
        slot is free, the node is idle again, and the frame goes back in the queue if we popped it."""
        g = self.open_grant
        if g is not None:
            self.revoked_from.setdefault(g["slot_id"], set()).add(g["node_id"])
        if st.in_flight is not None:
            if st.in_flight[0] not in st.queue:
                st.queue = st.with_frame(st.in_flight)
            st.in_flight = None
        self.open_grant = None
        self.grant_t = None

    # -- entry points
    def feed_line(self, line: str) -> None:
        self.n_lines += 1
        line = line.strip()
        if not line:
            return
        try:
            ev = json.loads(line)
        except ValueError as e:
            self.bad_lines += 1
            self.E(None, f"line {self.n_lines}: not JSON ({e})")
            return
        if not isinstance(ev, dict):
            self.bad_lines += 1
            self.E(None, f"line {self.n_lines}: not a JSON object")
            return
        self.feed(ev)

    def feed(self, ev: Event) -> None:
        seq: Any = ev.get("seq")
        # envelope
        if not is_type(seq, Int):
            self.E(seq, f"seq missing or not an int: {seq!r}")
        elif seq != self.seq_expected:
            if seq > self.seq_expected:
                self.E(seq, f"seq skipped {self.seq_expected}..{seq - 1} ({seq - self.seq_expected} missing)")
            else:
                self.E(seq, f"seq went backwards (expected {self.seq_expected})")
            self.seq_expected = seq + 1
        else:
            self.seq_expected += 1
        t: Any = ev.get("t")
        if not is_type(t, Num):
            self.E(seq, f"t missing or not a number: {t!r}")
            t = self.last_t
        elif self.last_t is not None and t < self.last_t:
            self.E(seq, f"t went backwards: {t} after {self.last_t}")
        typ: Any = ev.get("type")
        if not is_type(typ, Str):
            self.E(seq, f"type missing or not a string: {typ!r}")
            return
        if self.ended:
            self.E(seq, f"{typ} after run_end")
        if typ not in SCHEMA:
            self.unknown_types[typ] += 1
            if self.unknown_types[typ] == 1:
                self.W(seq, f"unknown event type {typ!r} (the display ignores it)")
            self.last_t = t if t is not None else self.last_t
            return
        self.types[typ] += 1
        if self.run is None and typ != "run_start" and self.types.total() == 1:
            self.E(seq, f"first event is {typ}, not run_start")
        # field shapes
        shape_ok = True
        for field, spec in SCHEMA[typ].items():
            if field not in ev:
                self.E(seq, f"{typ}: missing field {field!r}")
                shape_ok = False
            elif not is_type(ev[field], spec):
                self.E(seq, f"{typ}: field {field!r} should be {spec.rstrip('?')}, got {ev[field]!r}")
                shape_ok = False
        if shape_ok:
            # ground_status and bus_health are shape-checked and counted; nothing in the stream
            # cross-references them, so there is no per-event handler to run
            handler = getattr(self, "on_" + typ, None)
            if handler is not None:
                handler(seq, t, ev)
        if t is not None:
            self.last_t = t

    # -- per type
    def on_run_start(self, seq: int | None, t: float | None, ev: Event) -> None:
        if self.run is not None:
            self.E(seq, "second run_start in one file")
        self.run = ev
        if ev["mode"] not in MODES:
            self.E(seq, f"run_start: mode {ev['mode']!r} not in {MODES}")
        for n in ev["nodes"]:
            if not isinstance(n, dict) or not is_type(n.get("node_id"), Int):
                self.E(seq, f"run_start: bad node entry {n!r}")
                continue
            for f, spec in (("label", Str), ("transport", Str), ("real", Bool)):
                if not is_type(n.get(f), spec):
                    self.E(seq, f"run_start: node {n.get('node_id')} field {f!r} should be {spec}")
            if n["node_id"] in self.nodes and self.nodes[n["node_id"]].label != f"node {n['node_id']}":
                self.E(seq, f"run_start: duplicate node_id {n['node_id']}")
            self.nodes[n["node_id"]] = NodeState(n.get("label") or f"node {n['node_id']}")
        w = ev["window"]
        if not is_type(w.get("budget_bytes"), Int) or not is_type(w.get("duration_s"), Num):
            self.E(seq, f"run_start: window needs budget_bytes (int) and duration_s (number): {w!r}")
        else:
            self.budget, self.duration = w["budget_bytes"], w["duration_s"]
        self.queue_limit = ev["queue_limit"]
        if self.queue_limit < 1:
            self.E(seq, f"run_start: queue_limit {self.queue_limit} < 1")
        rule = ev["usable_rule"]
        if rule.get("metric") == "cloud_frac" and is_type(rule.get("max"), Num):
            self.usable_max = rule["max"]
        else:
            self.W(seq, f"run_start: usable_rule {rule!r} is not on cloud_frac; usable flags will not be checked")

    def on_node_status(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        if st.last_status_t is not None and t is not None:
            st.max_quiet = max(st.max_quiet, t - st.last_status_t)
        st.last_status_t = t
        st.link_ok = ev["link_ok"]
        self.resolve_depth(seq, st, ev["queue_depth"], "node_status")
        queue, sent = st.queue, st.n_sent
        sat = st.sat_queue()
        if sat is not None and self.ack_lag(
            seq,
            st,
            t,
            ev["queue_depth"] == len(st.queue) and ev["frames_sent"] == st.n_sent,
            ev["queue_depth"] == len(sat) and ev["frames_sent"] == st.n_sent - 1,
        ):
            queue, sent = sat, st.n_sent - 1
        depth = len(queue)
        if ev["queue_depth"] != depth:
            self.E(seq, f"node_status {st.label}: queue_depth {ev['queue_depth']} but the stream implies {depth}")
        head = head_of(queue)
        if head is None:
            if ev["top_frame_id"] not in EMPTY_TOP_ID:
                self.E(seq, f"node_status {st.label}: queue is empty but top_frame_id is {ev['top_frame_id']}")
            if ev["top_score"] != 0:
                self.E(seq, f"node_status {st.label}: queue is empty but top_score is {ev['top_score']}")
        else:
            if ev["top_frame_id"] != head[0]:
                self.E(seq, f"node_status {st.label}: top_frame_id {ev['top_frame_id']} but head of queue is {head[0]}")
            if ev["top_score"] != head[1]:
                self.E(seq, f"node_status {st.label}: top_score {ev['top_score']} but head score is {head[1]}")
            if t is not None:
                age = t - st.scored[head[0]]["t"]
                if ev["top_age_s"] < age - 1.0:
                    self.W(
                        seq,
                        f"node_status {st.label}: top_age_s {ev['top_age_s']} but its frame_scored was {age:.1f} s ago",
                    )
        if ev["frames_scored"] != st.n_scored:
            self.E(
                seq, f"node_status {st.label}: frames_scored {ev['frames_scored']} but {st.n_scored} frame_scored seen"
            )
        if ev["frames_sent"] != sent:
            self.E(seq, f"node_status {st.label}: frames_sent {ev['frames_sent']} but {sent} frame_arrived seen")
        if ev["frames_evicted"] != st.n_evicted:
            self.W(
                seq,
                f"node_status {st.label}: frames_evicted {ev['frames_evicted']}; counting non-null "
                f"evicted_frame_id gives {st.n_evicted} (is a rejected frame counted?)",
            )
        g = self.open_grant
        open_grant = g if g is not None and g.get("node_id") == ev["node_id"] else None
        holding = open_grant is not None
        if ev["busy"] != holding:
            self.W(
                seq,
                f"node_status {st.label}: busy={ev['busy']} while "
                f"{'holding slot ' + str(open_grant['slot_id']) if open_grant is not None else 'not transmitting'} "
                "(display reads busy as 'transmitting')",
            )

    def on_queue_window(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        self.resolve_depth(seq, st, ev["depth"], "queue_window")
        queue = st.queue
        sat = st.sat_queue()
        if sat is not None and self.ack_lag(seq, st, t, ev["depth"] == len(st.queue), ev["depth"] == len(sat)):
            queue = sat
        depth = len(queue)
        if ev["depth"] != depth:
            self.E(seq, f"queue_window {st.label}: depth {ev['depth']} but the stream implies {depth}")
        top = ev["top"]
        if len(top) > WINDOW_CAP:
            self.E(seq, f"queue_window {st.label}: {len(top)} entries, cap is {WINDOW_CAP}")
        ranked = ranked_of(queue)
        prev = None
        for i, e in enumerate(top):
            if (
                not isinstance(e, dict)
                or not is_type(e.get("frame_id"), Int)
                or not is_type(e.get("score"), Num)
                or not is_type(e.get("age_s"), Num)
            ):
                self.E(seq, f"queue_window {st.label}: bad entry {e!r}")
                continue
            if e["frame_id"] not in queue:
                self.E(seq, f"queue_window {st.label}: frame {e['frame_id']} is not in the queue")
            elif e["score"] != queue[e["frame_id"]]:
                self.E(
                    seq,
                    f"queue_window {st.label}: frame {e['frame_id']} score {e['score']}"
                    f" != scored {queue[e['frame_id']]}",
                )
            if prev is not None and e["score"] > prev:
                self.E(seq, f"queue_window {st.label}: top is not sorted by score (entry {i})")
            prev = e["score"]
            if i < len(ranked) and ranked[i][0] != e["frame_id"] and (i == 0 or ranked[i][1] != e["score"]):
                self.E(
                    seq,
                    f"queue_window {st.label}: entry {i} is frame {e['frame_id']},"
                    f" the queue's rank {i} is {ranked[i][0]}",
                )

    def on_frame_scored(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        fid, score = ev["frame_id"], ev["score"]
        if fid in st.scored:
            self.E(seq, f"frame_scored {st.label}: frame {fid} scored twice")
        self.frame_owner[(ev["node_id"], fid)] = ev["node_id"]
        if not 0.0 <= ev["cloud_frac"] <= 1.0:
            self.E(seq, f"frame_scored {st.label}: cloud_frac {ev['cloud_frac']} outside 0..1")
        for p in ("clear", "sharp", "change"):
            if not is_type(ev["parts"].get(p), Num):
                self.E(seq, f"frame_scored {st.label}: parts.{p} missing or not a number")
        st.scored[fid] = {"t": t, "score": score, "cloud_frac": ev["cloud_frac"]}
        st.n_scored += 1
        # the depth this event reports is after its own insert: undo that to compare with the model
        self.resolve_depth(
            seq, st, ev["queue_depth"] - (1 if ev["queued"] and ev["evicted_frame_id"] is None else 0), "frame_scored"
        )
        ev_id = ev["evicted_frame_id"]
        if ev["queued"]:
            if ev_id is not None:
                if ev_id == fid:
                    self.E(seq, f"frame_scored {st.label}: queued=true but evicted_frame_id is the frame itself")
                elif ev_id not in st.queue:
                    self.E(seq, f"frame_scored {st.label}: evicted frame {ev_id} was not in the queue")
                else:
                    if self.queue_limit is not None and len(st.queue) < self.queue_limit:
                        self.W(
                            seq,
                            f"frame_scored {st.label}: evicted {ev_id} while the queue had room "
                            f"({len(st.queue)}/{self.queue_limit})",
                        )
                    del st.queue[ev_id]
                    st.n_evicted += 1
            st.queue[fid] = score
        else:
            if ev_id is not None and ev_id != fid:
                self.E(seq, f"frame_scored {st.label}: queued=false but evicted_frame_id {ev_id} is another frame")
            if ev_id is not None:
                st.n_evicted += 1
            st.n_rejected += 1
            if self.queue_limit is not None and len(st.queue) < self.queue_limit:
                self.W(
                    seq,
                    f"frame_scored {st.label}: frame {fid} rejected while the queue had room "
                    f"({len(st.queue)}/{self.queue_limit})",
                )
        if self.queue_limit is not None and len(st.queue) > self.queue_limit:
            self.E(seq, f"frame_scored {st.label}: queue depth {len(st.queue)} exceeds queue_limit {self.queue_limit}")
        depth = len(st.queue)
        if st.ack_pending is not None and self.ack_lag(
            seq, st, t, ev["queue_depth"] == depth, ev["queue_depth"] == depth + 1
        ):
            depth += 1
        if ev["queue_depth"] != depth:
            self.E(seq, f"frame_scored {st.label}: queue_depth {ev['queue_depth']} but the stream implies {depth}")

    def on_grant(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        if self.open_grant is not None:
            self.W(
                seq,
                f"grant slot {ev['slot_id']} while slot {self.open_grant['slot_id']} "
                f"(node {self.open_grant['node_id']}) has no frame_arrived yet",
            )
        # "slot_id is the ground's round id (unique, increasing; a revoked slot re-uses it with a
        # new grant)": re-using the slot of a revoked grant is the re-arbitration, not a repeat.
        revoked = self.revoked_from.get(ev["slot_id"], set())
        if self.last_slot is not None and ev["slot_id"] <= self.last_slot and not revoked:
            self.W(seq, f"grant: slot_id {ev['slot_id']} does not increase (last {self.last_slot})")
        if ev["node_id"] in revoked:
            self.E(
                seq,
                f"grant: slot {ev['slot_id']} re-granted to node {ev['node_id']}, "
                "whose grant on that slot was revoked (the re-run sets its bid aside)",
            )
        self.last_slot = ev["slot_id"]
        if ev["reason"] not in REASONS:
            self.E(seq, f"grant: reason {ev['reason']!r} not in {REASONS}")
        self.reasons[ev["reason"]] += 1
        if self.last_window is not None and not self.last_window["open"]:
            self.W(seq, f"grant slot {ev['slot_id']} while the last window_update said open=false")
        bids = {}
        for b in ev["bids"]:
            if (
                not isinstance(b, dict)
                or not is_type(b.get("node_id"), Int)
                or not is_type(b.get("top_score"), Num)
                or not is_type(b.get("ready"), Bool)
            ):
                self.E(seq, f"grant: bad bid {b!r}")
                continue
            bids[b["node_id"]] = b
        missing = set(self.nodes) - set(bids)
        if missing:
            self.W(seq, f"grant: no bid listed for node(s) {sorted(missing)} (display shows all nodes side by side)")
        win = bids.get(ev["node_id"])
        if win is None:
            self.E(seq, f"grant: winner node {ev['node_id']} is not among the bids")
        elif not win["ready"]:
            self.E(seq, f"grant: winner node {ev['node_id']} bid ready=false")
        if st.link_ok is False:
            self.W(seq, f"grant to {st.label} whose last node_status said link_ok=false")
        if not st.queue:
            self.E(seq, f"grant to {st.label} whose queue is empty in the stream")
        ready = sorted((b for b in bids.values() if b["ready"]), key=lambda b: (-b["top_score"], b["node_id"]))
        if ready and win is not None:
            best = ready[0]
            if ev["reason"] == "only_ready" and len(ready) != 1:
                self.E(seq, f"grant: reason only_ready but {len(ready)} bidders were ready")
            if ev["reason"] == "highest_score":
                if win["top_score"] < best["top_score"]:
                    self.E(
                        seq,
                        f"grant: reason highest_score but node {best['node_id']} bid {best['top_score']} "
                        f"> winner's {win['top_score']}",
                    )
                elif best["node_id"] != ev["node_id"]:
                    self.W(seq, f"grant: tie at {win['top_score']} not broken by lowest node_id")
            if ev["reason"] == "starvation_forced":
                if best["node_id"] == ev["node_id"]:
                    self.E(seq, "grant: reason starvation_forced but the winner also holds the highest bid")
                elif not self.winners or self.winners[-1] != best["node_id"]:
                    self.W(
                        seq,
                        f"grant: starvation_forced but the top bidder (node {best['node_id']}) "
                        "did not win the previous slot",
                    )
        for b in bids.values():
            bs = self.nodes.get(b["node_id"])
            if bs is None or not b["ready"]:
                continue
            # A bid names its frame (event_stream.md, grant.bids[].frame_id). Hold the bid against
            # that frame's own score, not against the node's head now: bids are collected when the
            # round opens, and a re-arbitrated slot replays them after newer frames were scored.
            bf = b.get("frame_id")
            if bf is not None and bf in bs.scored:
                if b["top_score"] != bs.scored[bf]["score"]:
                    self.E(
                        seq,
                        f"grant: bid of {bs.label} for frame {bf} is {b['top_score']}"
                        f" but that frame was scored {bs.scored[bf]['score']}",
                    )
            elif bf is not None:
                self.E(seq, f"grant: bid of {bs.label} names frame {bf}, which it never frame_scored")
            elif bs.queue:
                h = bs.head()
                if h is not None and b["top_score"] != h[1]:
                    self.W(
                        seq, f"grant: bid of {bs.label} is {b['top_score']} but its head score in the stream is {h[1]}"
                    )
        self.winners.append(ev["node_id"])
        self.open_grant = ev
        self.grant_t = t
        st.in_flight = self.granted_frame(seq, st, ev)
        if self.pop_at == "grant" and st.in_flight is not None:
            del st.queue[st.in_flight[0]]

    def granted_frame(self, seq: int | None, st: NodeState, ev: Event) -> tuple[Any, float, int] | None:
        """Which frame this grant is for, and where it sits in the queue. The grant says so itself
        (event_stream.md, grant.frame_id); only a file that omits the field falls back to the head."""
        order = list(st.queue)
        fid = ev.get("frame_id")
        if fid is None:
            h = st.head()
            return None if h is None else (h[0], h[1], order.index(h[0]))
        if fid not in st.queue:
            # not a violation: `grant_unknown_item` is a registered anomaly (event_stream.md, fault 8).
            # A node can bid, evict the frame under memory pressure, then be granted it and fault.
            self.W(seq, f"grant to {st.label}: frame {fid} is not in its queue in the stream")
            return None
        return (fid, st.queue[fid], order.index(fid))

    def on_frame_arrived(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        fid = ev["frame_id"]
        g = self.open_grant
        if g is None:
            self.E(seq, f"frame_arrived slot {ev['slot_id']}: no grant is open")
        else:
            if g["slot_id"] != ev["slot_id"]:
                self.E(seq, f"frame_arrived slot {ev['slot_id']} but the open grant is slot {g['slot_id']}")
            if g["node_id"] != ev["node_id"]:
                self.E(
                    seq,
                    f"frame_arrived from node {ev['node_id']} but slot {g['slot_id']}"
                    f" was granted to node {g['node_id']}",
                )
            if t is not None and self.grant_t is not None and t - self.grant_t + 1e-6 < ev["duration_s"]:
                self.W(
                    seq,
                    f"frame_arrived: duration_s {ev['duration_s']} is longer than the time since the grant "
                    f"({t - self.grant_t:.2f} s)",
                )
        self.open_grant = None
        rec = st.scored.get(fid)
        if rec is None:
            self.E(seq, f"frame_arrived {st.label}: frame {fid} was never frame_scored by this node")
        else:
            if ev["score"] != rec["score"]:
                self.E(seq, f"frame_arrived {st.label}: frame {fid} score {ev['score']} != scored {rec['score']}")
            if abs(ev["cloud_frac"] - rec["cloud_frac"]) > 1e-6:
                self.W(
                    seq,
                    f"frame_arrived {st.label}: frame {fid} cloud_frac {ev['cloud_frac']}"
                    f" != scored {rec['cloud_frac']}",
                )
        flight = st.in_flight
        st.in_flight = None
        if flight is not None and flight[0] != fid:
            self.W(
                seq,
                f"frame_arrived {st.label}: sent frame {fid} but the grant was for {flight[0]} ({flight[1]})",
            )
        if fid in st.queue:
            held = (fid, st.queue[fid], list(st.queue).index(fid))
            del st.queue[fid]
            # the satellite only drops it when the tx_ack gets back: one more report may still count it
            st.ack_pending, st.ack_lag_t = held, None
        elif flight is None or flight[0] != fid:
            self.E(
                seq,
                f"frame_arrived {st.label}: frame {fid} is not in the queue (never queued, evicted, or already sent)",
            )
        self.check_usable(seq, "frame_arrived", st, ev)
        st.n_sent += 1
        self.n_arrived += 1
        self.n_usable += bool(ev["usable"])
        self.bytes_used += ev["bytes"]
        if self.budget is not None and self.bytes_used > self.budget:
            self.E(seq, f"frame_arrived: bytes used {self.bytes_used} exceed budget {self.budget}")

    def check_usable(self, seq: int | None, typ: str, st: NodeState, ev: Event) -> None:
        if self.usable_max is None:
            return
        expect = ev["cloud_frac"] <= self.usable_max
        if ev["usable"] != expect:
            self.E(
                seq,
                f"{typ} {st.label}: usable={ev['usable']} but cloud_frac {ev['cloud_frac']} "
                f"{'<=' if expect else '>'} usable_rule.max {self.usable_max} (usable must follow cloud_frac only)",
            )

    def on_baseline_arrival(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        fid = ev["frame_id"]
        key = (ev["node_id"], fid)
        owner = self.frame_owner.get(key)
        if owner is None:
            self.E(seq, f"baseline_arrival: {st.label} frame {fid} was never frame_scored by that node")
        else:
            rec = self.nodes[owner].scored[fid]
            if ev["score"] != rec["score"]:
                self.E(seq, f"baseline_arrival: frame {fid} score {ev['score']} != scored {rec['score']}")
        if key in self.base_sent:
            self.E(seq, f"baseline_arrival: {st.label} frame {fid} delivered twice")
        self.base_sent.add(key)
        self.check_usable(seq, "baseline_arrival", st, ev)
        self.base_n += 1
        self.base_usable += bool(ev["usable"])
        self.base_bytes += ev["bytes"]
        if self.budget is not None and self.base_bytes > self.budget:
            self.E(seq, f"baseline_arrival: baseline bytes {self.base_bytes} exceed budget {self.budget}")

    def on_window_update(self, seq: int | None, t: float | None, ev: Event) -> None:
        if self.budget is not None and ev["budget_bytes"] != self.budget:
            self.E(seq, f"window_update: budget_bytes {ev['budget_bytes']} != run_start budget {self.budget}")
        if ev["used_bytes"] != self.bytes_used:
            self.E(seq, f"window_update: used_bytes {ev['used_bytes']} but arrivals sum to {self.bytes_used}")
        if ev["remaining_bytes"] != ev["budget_bytes"] - ev["used_bytes"]:
            self.E(
                seq,
                f"window_update: remaining_bytes {ev['remaining_bytes']} != budget - used "
                f"({ev['budget_bytes'] - ev['used_bytes']})",
            )
        if ev["open"] and ev["remaining_bytes"] <= 0:
            self.E(seq, "window_update: open=true with no bytes remaining")
        if self.duration is not None and t is not None and abs(ev["time_remaining_s"] - (self.duration - t)) > 1.0:
            self.W(
                seq,
                f"window_update: time_remaining_s {ev['time_remaining_s']} but duration - t = {self.duration - t:.1f}",
            )
        if not ev["open"] and self.window_closed_t is None:
            self.window_closed_t = t
        self.last_window = ev

    def on_node_event(self, seq: int | None, t: float | None, ev: Event) -> None:
        st = self.node(seq, ev)
        if ev["level"] not in LEVELS:
            self.E(seq, f"node_event: level {ev['level']!r} not in {LEVELS}")
        self.levels[ev["level"]] += 1
        msg = ev["message"]
        if ev.get("node_id") is not None and JOINED.search(msg):
            self.announced.add(ev["node_id"])
        g = self.open_grant
        if g is not None and g["node_id"] == ev.get("node_id") and GRANT_ENDED.search(msg):
            self.end_grant(st)

    def on_run_end(self, seq: int | None, t: float | None, ev: Event) -> None:
        self.ended = True
        self.run_end = ev
        for side in ("orbit", "baseline"):
            for k in TOTALS:
                if not is_type(ev[side].get(k), Int):
                    self.E(seq, f"run_end: {side}.{k} missing or not an int")
        # the totals are cross-checked even when earlier events already failed
        o, b = ev["orbit"], ev["baseline"]
        checks = [
            ("orbit.frames_down", o.get("frames_down"), self.n_arrived),
            ("orbit.usable_down", o.get("usable_down"), self.n_usable),
            ("orbit.bytes_used", o.get("bytes_used"), self.bytes_used),
            (
                "orbit.frames_left_queued",
                o.get("frames_left_queued"),
                sum(len(n.queue) for n in self.nodes.values()),
            ),
            ("baseline.frames_down", b.get("frames_down"), self.base_n),
            ("baseline.usable_down", b.get("usable_down"), self.base_usable),
            ("baseline.bytes_used", b.get("bytes_used"), self.base_bytes),
        ]
        for name, got, want in checks:
            if got != want:
                self.E(seq, f"run_end: {name} is {got} but the events add up to {want}")
        h = ev["headline"]
        if h.get("metric") == "usable frames downlinked" and (
            h.get("orbit") != ev["orbit"].get("usable_down") or h.get("baseline") != ev["baseline"].get("usable_down")
        ):
            self.E(seq, "run_end: headline orbit/baseline do not equal the usable_down totals")
        if (
            is_type(h.get("orbit"), Num)
            and is_type(h.get("baseline"), Num)
            and is_type(h.get("gain"), Num)
            and h["baseline"] > 0
            and abs(h["gain"] - h["orbit"] / h["baseline"]) > 0.006
        ):
            self.E(
                seq,
                f"run_end: headline gain {h['gain']} != {h['orbit']}/{h['baseline']}"
                f" = {h['orbit'] / h['baseline']:.3f}",
            )
        if self.open_grant is not None:
            self.W(seq, f"run_end while slot {self.open_grant['slot_id']} has no frame_arrived")

    # -- wrap up
    def finish(self) -> Report:
        if self.run is None:
            self.E(None, "no run_start")
        elif not self.ended:
            self.W(None, "no run_end (partial run?)")
        for nid in sorted(self.late_joiners - self.announced):
            self.W(None, f"node {nid} joined after run_start but no node_event announced it")
        s = self.r.summary
        s["lines"] = self.n_lines
        s["bad_lines"] = self.bad_lines
        s["events"] = sum(self.types.values())
        s["types"] = dict(self.types)
        s["unknown_types"] = dict(self.unknown_types)
        s["duration_s"] = self.last_t
        s["run_id"] = self.run.get("run_id") if self.run else None
        s["nodes"] = {
            nid: {
                "label": n.label,
                "scored": n.n_scored,
                "queued_now": len(n.queue),
                "evicted": n.n_evicted,
                "rejected": n.n_rejected,
                "sent": n.n_sent,
                "max_quiet_s": round(n.max_quiet, 1),
            }
            for nid, n in self.nodes.items()
        }
        s["scored"] = sum(n.n_scored for n in self.nodes.values())
        s["arrived"] = self.n_arrived
        s["usable"] = self.n_usable
        s["bytes_used"] = self.bytes_used
        s["budget"] = self.budget
        s["window_closed_t"] = self.window_closed_t
        s["grants"] = dict(self.reasons)
        s["baseline"] = {"arrived": self.base_n, "usable": self.base_usable}
        s["node_events"] = dict(self.levels)
        s["headline"] = (self.run_end or {}).get("headline")
        return self.r


def check_lines(lines: Iterable[str]) -> Report:
    c = Checker()
    for line in lines:
        c.feed_line(line)
    return c.finish()


def check_events(events: Iterable[Event]) -> Report:
    c = Checker()
    for ev in events:
        c.feed(ev)
    return c.finish()


def format_summary(s: dict[str, Any], name: str) -> str:
    nodes = s.get("nodes", {})
    per = ", ".join(f"{n['label']} {n['scored']}" for n in nodes.values())
    quiet = max(nodes.values(), key=lambda n: n["max_quiet_s"], default=None)
    g = s.get("grants", {})
    lines = [
        f"{name}: {s.get('events', 0)} events over {s.get('duration_s') or 0:.1f} s, {len(nodes)} nodes"
        + (f", run_id {s['run_id']}" if s.get("run_id") else "")
        + (f", {s['bad_lines']} unparseable lines" if s.get("bad_lines") else ""),
        f"  scored {s.get('scored', 0)} ({per}) · still queued {sum(n['queued_now'] for n in nodes.values())}"
        f" · evicted {sum(n['evicted'] for n in nodes.values())}"
        f" (of which rejected on arrival {sum(n['rejected'] for n in nodes.values())})",
        f"  downlinked {s.get('arrived', 0)} (usable {s.get('usable', 0)}) · grants: "
        + (", ".join(f"{k} {v}" for k, v in sorted(g.items(), key=lambda kv: -kv[1])) or "none"),
        f"  baseline FIFO {s.get('baseline', {}).get('arrived', 0)} (usable {s.get('baseline', {}).get('usable', 0)})"
        + (
            f" · headline {s['headline'].get('orbit')} vs {s['headline'].get('baseline')}"
            f" = {s['headline'].get('gain')}x"
            if s.get("headline")
            else " · no run_end headline"
        ),
        f"  window {s.get('bytes_used', 0)} / {s.get('budget')} bytes"
        + (
            f", closed at t={s['window_closed_t']:.1f}"
            if s.get("window_closed_t") is not None
            else ", still open at the end"
        ),
        "  node_events "
        + (", ".join(f"{k} {v}" for k, v in s.get("node_events", {}).items()) or "none")
        + (f" · longest node silence {quiet['label']} {quiet['max_quiet_s']} s" if quiet else ""),
    ]
    if s.get("unknown_types"):
        lines.append(f"  unknown types ignored: {s['unknown_types']}")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    import argparse

    ap = argparse.ArgumentParser(description="Check a runs/<run_id>.jsonl file against docs/event_stream.md")
    ap.add_argument("path", help="run file, or - for stdin")
    ap.add_argument("--quiet", action="store_true", help="summary only, no per-event lines")
    ap.add_argument("--max", type=int, default=40, help="per-event lines to print per category (default 40)")
    args = ap.parse_args(argv)
    if args.path == "-":
        rep = check_lines(sys.stdin)
        name = "stdin"
    else:
        with open(args.path, encoding="utf-8") as f:
            rep = check_lines(f)
        name = args.path
    if not args.quiet:
        for tag, items in (("E", rep.errors), ("W", rep.warnings), ("N", rep.notes)):
            for seq, msg in items[: args.max]:
                print(f"{tag} seq {seq if seq is not None else '-':>5}  {msg}")
            if len(items) > args.max:
                print(f"{tag} ... {len(items) - args.max} more")
    print(format_summary(rep.summary, name))
    print(f"{'OK' if rep.ok else 'FAIL'}: {len(rep.errors)} errors, {len(rep.warnings)} warnings")
    return 0 if rep.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
