"""ScoringNode: the complete edge-node behaviour from protocol.md §3 and §5 as
pure Python/NumPy, driven by Message objects. SimulatedNode wraps it in a
NodeTransport so the orchestrator cannot tell it from an Arty.

Emission order (normative, RTL must match): FRAME_INGEST row 127 -> FRAME_SCORED.
GRANT -> TX_FRAME, TX_DONE (or TX_DONE alone). CONFIG_SET / STATUS_QUERY ->
STATUS_REPLY. BENCH_RUN -> BENCH_DONE. Timers may interleave between, never
inside, those sequences. Cycle counters are 0 here (§5.9).
"""
from __future__ import annotations

import time

import numpy as np

from orbit import params
from orbit.protocol import messages as M
from orbit.protocol.messages import FrameDecoder, encode
from orbit.protocol.transport import QueueTransport
from .queue import NO_FRAME, PriorityQueue
from .score import Config, score_frame

SHAPE = (params.FRAME_H, params.FRAME_W)


class ScoringNode:
    def __init__(self, node_id: int):
        self.node_id = node_id
        self.cfg = Config()
        self.config_valid = True
        self.frame = np.zeros(SHAPE, np.uint8)
        self.ref = np.zeros(SHAPE, np.uint8)
        self.frame_id = 0
        self.ref_loaded = False
        self.queue = PriorityQueue(self.cfg.queue_limit)
        self.state = M.StatusReply.IDLE
        # counters (u16, wrap)
        self.frames_scored = self.frames_sent = self.rows_rx = self.busy_drops = 0
        self.crc_errors = self.len_errors = self.unknown_type = self.rx_overflow = 0
        # link / timers
        self.link_ok = False
        self.tx_stalled = False
        self.ina219_present = False
        self.last_orch_hb_ms: int | None = None
        self.next_hb_ms = params.HEARTBEAT_MS
        self.next_power_ms = params.POWER_PERIOD_MS
        self.hb_seq = 0
        self.uptime_ms = 0
        self.last: object = None                 # last Scored, for the worked example / dashboard

    # ------------------------------------------------------------------ helpers
    def _bump(self, name):
        setattr(self, name, (getattr(self, name) + 1) & 0xFFFF)

    @property
    def busy(self) -> bool:
        return self.state != M.StatusReply.IDLE

    def _flags(self) -> int:
        S = M.StatusReply
        f = S.F_HAS_DATA if self.queue.has_data else 0
        f |= S.F_LINK_OK if self.link_ok else 0
        f |= S.F_CONFIG_VALID if self.config_valid else 0
        f |= S.F_REF_LOADED if self.ref_loaded else 0
        f |= S.F_BUSY if self.busy else 0
        f |= S.F_TX_STALLED if self.tx_stalled else 0
        f |= S.F_INA219_PRESENT if self.ina219_present else 0
        return f

    def status(self) -> M.StatusReply:
        top_score, top_id = self.queue.top
        return M.StatusReply(node_id=self.node_id, state=self.state, flags=self._flags(), queue_depth=len(self.queue),
                             top_score=top_score, top_frame_id=top_id, frames_scored=self.frames_scored,
                             frames_evicted=self.queue.evicted, frames_sent=self.frames_sent, rows_rx=self.rows_rx,
                             busy_drops=self.busy_drops, crc_errors=self.crc_errors, len_errors=self.len_errors,
                             unknown_type=self.unknown_type, rx_overflow=self.rx_overflow, cycles_last_frame=0)

    # ------------------------------------------------------------------ messages
    def handle(self, msg: M.Message, now_ms: int) -> list[M.Message]:
        self.uptime_ms = now_ms
        if isinstance(msg, M.Heartbeat):
            self.link_ok = True
            self.last_orch_hb_ms = now_ms
            return []
        if isinstance(msg, M.StatusQuery):
            return [self.status()]
        if isinstance(msg, M.ConfigSet):
            cfg = Config(msg.w_clear, msg.w_sharp, msg.w_change, msg.cloud_thr, msg.change_thr, msg.sharp_shift,
                         msg.queue_limit)
            if cfg.valid():
                self.cfg, self.config_valid = cfg, True
                self.queue.set_limit(cfg.queue_limit)
            else:
                self.config_valid = False
                self._bump("busy_drops")
            return [self.status()]
        if self.busy:                                # §3: rows, grants and bench while BUSY
            if isinstance(msg, (M.FrameIngest, M.RefFrameSet, M.Grant, M.BenchRun)):
                self._bump("busy_drops")
            return []
        if isinstance(msg, (M.FrameIngest, M.RefFrameSet)):
            if msg.row >= params.FRAME_H:
                self._bump("busy_drops")
                return []
            self._bump("rows_rx")
            if isinstance(msg, M.RefFrameSet):
                self.ref[msg.row, :] = msg.pixels
                if msg.row == params.FRAME_H - 1:
                    self.ref_loaded = True
                return []
            self.frame[msg.row, :] = msg.pixels
            self.frame_id = msg.frame_id
            if msg.row != params.FRAME_H - 1:
                return []
            return [self.score()]
        if isinstance(msg, M.Grant):
            return self.grant(msg)
        if isinstance(msg, M.BenchRun):
            # The kernel is content-independent; nothing to re-run in Python. cycles=0 (§5.9).
            return [M.BenchDone(node_id=self.node_id, iterations=msg.iterations, cycles=0)]
        return []

    def score(self) -> M.FrameScored:
        s = score_frame(self.frame, self.ref, self.cfg)
        self.last = s
        lost = self.queue.insert(s.score, self.frame_id)
        self._bump("frames_scored")
        return M.FrameScored(node_id=self.node_id, frame_id=self.frame_id, clear=s.clear, sharp=s.sharp,
                             change=s.change, score=s.score, evicted_id=lost, queue_depth=len(self.queue))

    def grant(self, g: M.Grant) -> list[M.Message]:
        out: list[M.Message] = []
        consumed = 0
        if self.queue.has_data and params.FRAME_BYTES <= g.budget_bytes:
            score, fid = self.queue.pop()
            self._bump("frames_sent")
            consumed = params.FRAME_BYTES
            out.append(M.TxFrame(node_id=self.node_id, slot_id=g.slot_id, frame_id=fid, score=score,
                                 byte_count=consumed))
        out.append(M.TxDone(node_id=self.node_id, slot_id=g.slot_id, bytes_consumed=consumed,
                            new_top_score=self.queue.top[0],
                            flags=M.TxDone.F_HAS_DATA if self.queue.has_data else 0))
        return out

    # ------------------------------------------------------------------ timers
    def tick(self, now_ms: int) -> list[M.Message]:
        """Heartbeat + status every HEARTBEAT_MS, power every POWER_PERIOD_MS, link timeout."""
        self.uptime_ms = now_ms
        out: list[M.Message] = []
        if self.last_orch_hb_ms is not None and now_ms - self.last_orch_hb_ms > params.LINK_TIMEOUT_MS:
            self.link_ok = False
        if now_ms >= self.next_hb_ms:
            self.next_hb_ms = now_ms + params.HEARTBEAT_MS
            out.append(M.Heartbeat(sender=self.node_id, seq=self.hb_seq, uptime_ms=now_ms & 0xFFFFFFFF))
            self.hb_seq = (self.hb_seq + 1) & 0xFFFF
            out.append(self.status())
        if now_ms >= self.next_power_ms:
            self.next_power_ms = now_ms + params.POWER_PERIOD_MS
            out.append(M.Power(node_id=self.node_id, flags=0, bus_raw=0, shunt_raw=0,
                               uptime_ms=now_ms & 0xFFFFFFFF))   # simulated node has no INA219: valid=0
        return out


class SimulatedNode(QueueTransport):
    """ScoringNode behind the NodeTransport interface, speaking real frames.
    Frames are processed synchronously in send(); timers advance on recv()
    polls (ponytail: no thread — the orchestrator polls recv() anyway; add a
    thread only if a caller stops polling)."""

    def __init__(self, node_id: int, clock=None):
        super().__init__()
        self.node = ScoringNode(node_id)
        self.decoder = FrameDecoder()
        self.clock = clock or (lambda: time.monotonic() * 1000.0)
        self.t0 = self.clock()
        self.from_node.put(b"\x00")   # link-open delimiter

    def now_ms(self) -> int:
        return int(self.clock() - self.t0)

    def _emit(self, msgs):
        for m in msgs:
            self.from_node.put(encode(m))

    def send(self, frame: bytes) -> None:
        now = self.now_ms()
        for msg in self.decoder.feed(frame):
            self._emit(self.node.handle(msg, now))
        # mirror decoder counters into the node's STATUS, like the RTL framer does
        n = self.node
        n.crc_errors, n.len_errors, n.unknown_type, n.rx_overflow = (
            self.decoder.crc_errors, self.decoder.len_errors, self.decoder.unknown_type, self.decoder.rx_overflow)

    def recv(self, timeout: float) -> bytes | None:
        self._emit(self.node.tick(self.now_ms()))
        return super().recv(timeout)


def rows(frame: np.ndarray, frame_id: int, cls=M.FrameIngest) -> list[M.Message]:
    """The 128 messages that carry one frame (ground side helper)."""
    return [cls(frame_id=frame_id, row=y, pixels=bytes(frame[y])) for y in range(params.FRAME_H)]
