"""The monitoring display: a UDP telemetry sink with a browser view. Never in the control path.

    uv run python -m viz.server --port 8765 --telemetry-port 50010

The ground fire-and-forgets one JSON datagram per event (``orbit/ground/telemetry.py``).
Recorded runs arrive in the other shape — the ``type``-tagged event stream of
``docs/event_stream.md``, which is what ``runs/*.jsonl`` holds — and ``StreamView`` below
translates those back into telemetry, as far as the translation is honest.
This process listens for both, keeps a bounded memory of what it has seen, and serves
``viz/static/index.html`` plus ``GET /api/state`` and ``WS /ws``. Nothing here talks back
to the ground: if this process is slow or dead the ground drops telemetry and carries on,
so every structure is bounded and every event is treated as possibly missing, duplicated
or out of order. The page holds no logic that a judge could mistake for arbitration; the
one derived thing computed here — joining a decision to its later outcome — is done from
the same events and is unit-tested.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import logging
import signal
import time
from collections import Counter, deque
from collections.abc import Iterator
from pathlib import Path
from typing import Any

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse

from orbit.config import DEFAULTS
from orbit.log import log

lg = logging.getLogger("orbit.display")
STATIC = Path(__file__).parent / "static"
DECISIONS_KEPT = 50
RECENT_KEPT = 200
BUS_KEPT = 100
CHUNK_LOOKBACK = 8  # bus lines to search for the running tx_chunk aggregate of the same item
CLIENT_QUEUE = 512  # pushes a browser may fall behind by before it is dropped rather than slowing ingest
RATE_KEYS = ("item_aging_rate", "sat_aging_rate")
SNAPSHOT_S = 1.0  # ground seconds between synthesized snapshots, the cadence the ground emits its own at

Event = dict[str, Any]


class StreamView:
    """Event-stream lines (``type``) turned back into the telemetry (``kind``) the panels render.

    ``orbit/ground/stream.py`` builds the event stream *from* telemetry: telemetry is upstream and
    the stream is the curated view of it, so this class is that projection run backwards. It is run
    backwards only as far as it honestly goes. The projection threw things away — the whole ground
    state every second became one event per node per bid — so the per-node accumulation below
    rebuilds what a ``snapshot`` needs, and the periodic ``ground_status`` (the stream's ground
    heartbeat) carries it out at the same 1 Hz the telemetry ``snapshot`` used.

    What the ground never put in the stream is not invented here. ``node_event`` is prose — the
    structured ``revoke`` / ``eviction`` / ``late_bid`` the events feed needs is gone, and parsing
    it back out of an English sentence would be fabrication — so it, ``frame_scored``,
    ``baseline_arrival`` and ``run_end`` are accepted, counted in ``by_type``, and rendered
    nowhere. The stream carries no bus mirror, no flag assessments and no per-node buffer or wait
    figures, so those panels stay empty on a replayed run rather than showing a made-up number.
    """

    def __init__(self) -> None:
        self.reset("")

    def reset(self, run_id: str) -> None:
        self.run_id = run_id or "replay"
        self.roster: dict[int, str] = {}  # node_id -> label; grant bids carry only the id
        self.sats: dict[str, Event] = {}
        self.window: Event = {}
        self.granted_to = ""
        self.next_snapshot = 0.0
        self.heartbeat = False  # has this run ever sent a ground_status?

    def feed(self, e: Event) -> list[Event]:
        """One event-stream line in, the telemetry events it honestly becomes out — often none."""
        nid, label = e.get("node_id"), e.get("label")
        if isinstance(nid, int) and isinstance(label, str):
            self.roster[nid] = label
        match str(e.get("type")):
            case "run_start":
                return self._run_start(e)
            case "ground_status":
                self.heartbeat = True
                return [self._snapshot(e)]
            case "bus_health":
                return [self._bus_stats(e)]
            case "grant":
                return [self._decision(e)]
            case "frame_arrived":
                return [self._complete(e)]
            case "node_status":
                self._node_status(e)
            case "queue_window":
                self._queue_window(e)
            case "window_update":
                self._window(e)
            case _:
                return []
        # A run recorded before ``ground_status`` existed carries no ground heartbeat, so the node
        # state accumulated above would never reach the page. Flush it at the same cadence instead.
        # One ground_status and this stops for good: a run that has a heartbeat gets the ground's
        # own snapshots — state, round id and all — and never a hollow one racing them at equal t.
        if self.heartbeat or _t(e) < self.next_snapshot:
            return []
        self.next_snapshot = _t(e) + SNAPSHOT_S
        return [self._snapshot({"t": _t(e)})]

    # --- the events that carry no telemetry of their own, only state for the next snapshot ---

    def _run_start(self, e: Event) -> list[Event]:
        self.reset(str(e.get("run_id", "")))  # a looped replay starts the run over from scratch
        nodes = e.get("nodes")
        for n in nodes if isinstance(nodes, list) else []:
            if isinstance(n, dict) and isinstance(n.get("node_id"), int) and isinstance(n.get("label"), str):
                self.roster[n["node_id"]] = n["label"]
        w = _obj(e.get("window"))
        self.window = {k: w[s] for s, k in (("duration_s", "duration_s"), ("budget_bytes", "capacity_bytes")) if s in w}
        rates = {k: v for k, v in _obj(e.get("priority")).items() if k in RATE_KEYS and isinstance(v, int | float)}
        # The aging rates are the one thing run_start carries that the page reads, and RATE_KEYS is
        # harvested off any event whatever its kind. No ground event carries them today, so there is
        # no kind to borrow: `settings` is a carrier the panels ignore, as the rates test already uses.
        return [self._ev("settings", e, **rates)] if rates else []

    def _node_status(self, e: Event) -> None:
        h = self._label(e)
        if not h:
            return
        s = self.sats.setdefault(h, {})
        for src, dst in (("queue_depth", "queue_len"), ("top_score", "top_score"), ("frames_sent", "transmissions")):
            if isinstance(e.get(src), int | float):
                s[dst] = e[src]
        if isinstance(e.get("frames_evicted"), int):
            s["eviction_count"] = e["frames_evicted"]
        if e.get("busy") is True:
            self.granted_to = h
        elif self.granted_to == h:
            self.granted_to = ""

    def _queue_window(self, e: Event) -> None:
        h = self._label(e)
        if not h:
            return
        s = self.sats.setdefault(h, {})
        top = e.get("top")
        if isinstance(top, list):
            s["window"] = [[q.get("frame_id"), q.get("score"), q.get("age_s")] for q in top if isinstance(q, dict)]
        if isinstance(e.get("depth"), int):
            s["queue_len"] = e["depth"]

    def _window(self, w: Event) -> None:
        keys = ("used_bytes", "remaining_bytes", "slots_used", "slots_remaining", "open", "time_remaining_s")
        self.window.update({k: w[k] for k in keys if k in w})
        if "budget_bytes" in w:
            self.window["capacity_bytes"] = w["budget_bytes"]

    # --- the events that do become telemetry ---------------------------------------------

    def _snapshot(self, e: Event) -> Event:
        """``ground_status`` is the stream's periodic ground heartbeat and ``snapshot`` is
        telemetry's, at the same period and carrying the same state, round count and window
        accounting. ``nodes.seen`` / ``nodes.linked`` / ``uptime_s`` have no telemetry home and are
        dropped; so are the ground's own counters past ``rounds``, which the stream never carried."""
        self._window(_obj(e.get("window")))
        w = dict(self.window)
        used, left = w.get("slots_used"), w.get("slots_remaining")
        if isinstance(used, int) and isinstance(left, int):
            w["slots_total"] = used + left
        cap, dur = w.get("capacity_bytes"), w.get("duration_s")
        if isinstance(cap, int | float) and isinstance(dur, int | float) and dur > 0:
            w["rate_bps"] = cap * 8.0 / dur  # exact: the ground sized the budget from this rate
        # `scaled` is not derivable from anything the stream carries and is left out rather than
        # guessed; see the note to viz/static/index.html's owner about the badge that implies.
        rounds = e.get("rounds")
        return self._ev(
            "snapshot",
            e,
            state=e.get("state"),
            round_id=rounds,
            granted_to=self.granted_to,
            window=w,
            counters={"rounds": rounds},
            sats={h: dict(s) for h, s in sorted(self.sats.items())},
        )

    def _bus_stats(self, e: Event) -> Event:
        """Exact: ``bus_health``'s received/delivered/dropped are the ground bus's ``BusStats``
        fields, which is what the telemetry ``bus_stats`` event is. The rates, the sliding window
        and the alerts are computed in the stream and have nowhere to go here."""
        d = _obj(e.get("dropped"))
        return self._ev(
            "bus_stats",
            e,
            received=e.get("received"),
            delivered=e.get("delivered"),
            dropped_malformed=d.get("malformed"),
            dropped_dup=d.get("dup"),
            dropped_oversize=d.get("oversize"),
            dropped_own=d.get("own"),
            malformed_by_type=e.get("malformed_by_type"),
        )

    def _decision(self, e: Event) -> Event:
        raw = e.get("bids")
        bids = [b for b in raw if isinstance(b, dict) and b.get("ready")] if isinstance(raw, list) else []
        winner = self._label(e)
        ranked: list[Event] = [
            {
                "sat": self._label(b),
                "item_id": b.get("frame_id"),
                "score": b.get("top_score"),
                "item_age_s": b.get("item_age_s"),
                "item_age_term": b.get("item_age_term"),
                "sat_wait_s": b.get("sat_wait_s"),
                "sat_wait_term": b.get("sat_wait_term"),
                "total": b.get("priority"),
            }
            for b in bids
        ]
        # the stream lists bidders in node_id order; telemetry ranks them, winner first
        ranked.sort(key=lambda c: (c["sat"] != winner, -_num(c["total"])))
        self.granted_to = winner
        return self._ev(
            "decision",
            e,
            round_id=e.get("slot_id"),
            winner=winner,
            item_id=e.get("frame_id"),
            margin=e.get("margin"),
            excluded=[],  # the stream never carried the excluded set
            ranked=ranked,
        )

    def _complete(self, e: Event) -> Event:
        self.granted_to = ""
        return self._ev(
            "complete",
            e,
            round_id=e.get("slot_id"),
            sat=self._label(e),
            item_id=e.get("frame_id"),
            score=e.get("score"),
            bytes=e.get("bytes"),
            cloud_frac=e.get("cloud_frac"),
        )

    # --- helpers --------------------------------------------------------------------------

    def _ev(self, kind: str, src: Event, **body: Any) -> Event:
        return {"kind": kind, "host": self.run_id, "t": _t(src), **body}

    def _label(self, e: Event) -> str:
        nid = e.get("node_id")
        return self.roster.get(nid, f"node-{nid}") if isinstance(nid, int) else ""


class Store:
    """Everything the display remembers. Pure and synchronous so it can be unit-tested by calling
    ``ingest`` with bytes; the network layers below only feed it and read from it."""

    def __init__(self, backlog: int = 2000) -> None:
        self.events: deque[Event] = deque(maxlen=backlog)
        self.recent: deque[Event] = deque(maxlen=RECENT_KEPT)
        self.bus: deque[Event] = deque(maxlen=BUS_KEPT)
        self.decisions: deque[Event] = deque(maxlen=DECISIONS_KEPT)
        self.snapshot: Event | None = None
        self.flags: dict[str, Any] = {}
        self.rates: dict[str, float] | None = None
        self.telemetry_stats: Event | None = None
        self.bus_stats: Event | None = None
        self.by_kind: Counter[str] = Counter()
        self.by_type: Counter[str] = Counter()  # event-stream lines, whether or not they render
        self.stream = StreamView()
        self.datagrams = 0
        self.bad_datagrams = 0
        self.restarts = 0
        self.max_round = -1
        self.last_event_t: float | None = None
        self.last_rx_wall: float | None = None
        self.subscribers: set[asyncio.Queue[str]] = set()

    # --- ingest -------------------------------------------------------------------------

    def ingest(self, data: bytes) -> None:
        """One datagram in. Garbage is counted, never raised: a bad byte from the ground must not
        take the display down mid-demo.

        Two envelopes land here. Telemetry (``kind``) is what the live ground unicasts and what
        every panel is written against. An event-stream line (``type``, ``docs/event_stream.md``,
        and what a recorded ``runs/*.jsonl`` holds) goes through ``StreamView`` instead. ``kind``
        is tested first, because a telemetry ``bus`` event carries a bus-message ``type`` of its
        own and must never be mistaken for a stream line.

        ``bad_datagrams`` means the same thing it always did — not JSON, not an object, or neither
        tag a string — so real corruption is still caught. An unknown ``type`` is accepted and
        rendered nowhere, exactly as an unknown ``kind`` already was."""
        self.datagrams += 1
        try:
            event = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            self.bad_datagrams += 1
            return
        if not isinstance(event, dict):
            self.bad_datagrams += 1
            return
        if isinstance(event.get("kind"), str):
            out = [event]
        elif isinstance(event.get("type"), str):
            self.by_type[str(event["type"])] += 1
            out = self.stream.feed(event)
        else:
            self.bad_datagrams += 1
            return
        # the line arrived, so the ground is alive, whether or not this display can draw it: the
        # watchdog on the page is about hearing from the ground, not about any one panel.
        self.last_rx_wall = time.monotonic()
        for e in out:
            self._publish(self.apply(e))

    def _publish(self, push: Event) -> None:
        if self.subscribers:
            text = json.dumps(push, default=str)
            for q in list(self.subscribers):
                try:
                    q.put_nowait(text)
                except asyncio.QueueFull:
                    self.subscribers.discard(q)  # a stalled browser is dropped; ingest never waits

    def apply(self, e: Event) -> Event:
        kind = str(e["kind"])
        self.by_kind[kind] += 1
        self.events.append(e)
        reset = self._check_restart(kind, e)
        t = e.get("t")
        if isinstance(t, int | float):
            self.last_event_t = float(t)
        push: Event = {"type": "event", "event": e}
        if reset:
            push["reset"] = True
        if kind == "bus":
            self._bus(e)
        else:
            self.recent.append(e)
        if kind == "snapshot":
            if self.snapshot is None or reset or _t(e) >= _t(self.snapshot):
                self.snapshot = e  # an older snapshot arriving late must not roll the header back
                rid = e.get("round_id")
                if isinstance(rid, int):
                    self.max_round = max(self.max_round, rid)
        elif kind == "flags" and isinstance(e.get("sats"), dict):
            self.flags = e["sats"]
        elif kind == "telemetry_stats":
            self.telemetry_stats = e
        elif kind == "bus_stats":
            self.bus_stats = e
        elif kind in ("decision", "complete", "tx_failed", "revoke"):
            self._decisions(kind, e)
            push["decisions"] = list(self.decisions)
        found = {k: e[k] for k in RATE_KEYS if isinstance(e.get(k), int | float)}
        if found:
            self.rates = {**(self.rates or {}), **{k: float(v) for k, v in found.items()}}
            push["rates"] = self.rates
        return push

    def _check_restart(self, kind: str, e: Event) -> bool:
        """A ground restart shows up as round_id going backwards on the events that carry the
        *current* round, and as ground time (seconds since its start) jumping back. One step of
        UDP reordering is tolerated; more than that is a new ground."""
        if kind not in ("round_open", "decision", "snapshot"):
            return False
        rid = e.get("round_id")
        if not isinstance(rid, int):
            return False
        t_back = self.last_event_t is not None and _t(e) < self.last_event_t - 5.0
        if rid < self.max_round - 1 or (t_back and rid <= self.max_round):
            log(lg, logging.WARNING, "ground_restart", seen_round=rid, previous_round=self.max_round)
            self.restarts += 1
            self.decisions.clear()
            self.flags = {}
            self.snapshot = None
            self.max_round = rid
            return True
        self.max_round = max(self.max_round, rid)
        return False

    def _bus(self, e: Event) -> None:
        """tx_chunk arrives ~50/s during a transmission; one line per item keeps the log readable."""
        if e.get("type") == "tx_chunk":
            for prev in list(self.bus)[-CHUNK_LOOKBACK:]:  # heartbeats interleave; still one line per item
                if prev.get("type") == "tx_chunk" and (prev.get("from"), prev.get("item_id")) == (
                    e.get("from"),
                    e.get("item_id"),
                ):
                    prev["count"] = int(prev.get("count", 1)) + 1
                    prev["t"] = e.get("t", prev.get("t"))
                    return
            e = {**e, "count": 1}
        else:
            self.recent.append(e)
        self.bus.append(e)

    def _decisions(self, kind: str, e: Event) -> None:
        """Join each decision to its outcome. ``revoke`` carries the round; ``complete`` and
        ``tx_failed`` carry only (sat, item_id), so they match the newest pending grant for that pair."""
        if kind == "decision":
            raw = e.get("ranked")
            ranked: list[Any] = [c for c in raw if isinstance(c, dict)] if isinstance(raw, list) else []
            key = (e.get("round_id"), e.get("t"))
            if any((d["round_id"], d["t"]) == key for d in self.decisions):
                return  # duplicate datagram
            win: dict[str, Any] = ranked[0] if ranked else {}
            ru: dict[str, Any] | None = ranked[1] if len(ranked) > 1 else None
            self.decisions.append(
                {
                    "round_id": e.get("round_id"),
                    "t": e.get("t"),
                    "winner": e.get("winner", win.get("sat")),
                    "item_id": e.get("item_id", win.get("item_id")),
                    "score": win.get("score"),
                    "item_age_term": win.get("item_age_term"),
                    "sat_wait_term": win.get("sat_wait_term"),
                    "total": win.get("total"),
                    "runner_up": ru.get("sat") if ru else None,
                    "runner_total": ru.get("total") if ru else None,
                    "margin": e.get("margin"),
                    "excluded": e.get("excluded", []),
                    "ranked": ranked,
                    "outcome": "pending",
                    "reason": "",
                }
            )
            return
        if not isinstance(e.get("sat"), str):
            return  # an outcome with no satellite cannot be joined to anything
        for d in reversed(self.decisions):
            if d["outcome"] != "pending" or d["winner"] != e.get("sat"):
                continue
            if kind == "revoke":
                if d["round_id"] != e.get("round_id"):
                    continue
            elif d["item_id"] != e.get("item_id"):
                continue
            d["outcome"] = {"complete": "complete", "tx_failed": "failed", "revoke": "revoked"}[kind]
            d["reason"] = str(e.get("reason", ""))
            return

    # --- read side ----------------------------------------------------------------------

    def state(self, clients: int = 0) -> Event:
        age = None if self.last_rx_wall is None else round(time.monotonic() - self.last_rx_wall, 2)
        return {
            "snapshot": self.snapshot,
            "decisions": list(self.decisions),
            "recent_events": list(self.recent),
            "bus": list(self.bus),
            "flags": self.flags,
            "rates": self.rates,
            "telemetry_stats": self.telemetry_stats,
            "bus_stats": self.bus_stats,
            "stats": {
                "datagrams": self.datagrams,
                "bad_datagrams": self.bad_datagrams,
                "by_kind": dict(self.by_kind),
                "by_type": dict(self.by_type),
                "connected_clients": clients,
                "age_s": age,
                "restarts": self.restarts,
                "last_event_t": self.last_event_t,
            },
        }

    @contextlib.contextmanager
    def subscribe(self) -> Iterator[asyncio.Queue[str]]:
        q: asyncio.Queue[str] = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.subscribers.add(q)
        try:
            yield q
        finally:
            self.subscribers.discard(q)


def _t(e: Event) -> float:
    return _num(e.get("t"))


def _num(v: Any) -> float:
    return float(v) if isinstance(v, int | float) else 0.0


def _obj(v: Any) -> Event:
    return v if isinstance(v, dict) else {}


class _Listener(asyncio.DatagramProtocol):
    def __init__(self, store: Store) -> None:
        self.store = store

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self.store.ingest(data)

    def error_received(self, exc: Exception) -> None:
        log(lg, logging.WARNING, "udp_error", error=str(exc))


def create_app(telemetry_port: int = DEFAULTS.telemetry_port, backlog: int = 2000) -> FastAPI:
    store = Store(backlog)
    clients: set[WebSocket] = set()
    loop_holder: dict[str, asyncio.AbstractEventLoop] = {}

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI) -> Any:
        loop = asyncio.get_running_loop()
        loop_holder["loop"] = loop
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _Listener(store), local_addr=("0.0.0.0", telemetry_port)
        )
        app.state.udp_port = transport.get_extra_info("sockname")[1]
        log(lg, logging.INFO, "display_start", telemetry_port=app.state.udp_port)
        try:
            yield
        finally:
            transport.close()

    def ingest(data: bytes) -> None:
        """Thread-safe entry for tests and for anything that already holds the datagram: runs on
        the app loop when one exists in another thread (the TestClient case), inline otherwise."""
        loop = loop_holder.get("loop")
        if loop is None or not loop.is_running() or _current_loop() is loop:
            store.ingest(data)
        else:
            asyncio.run_coroutine_threadsafe(_call(store.ingest, data), loop).result(timeout=5)

    app = FastAPI(lifespan=lifespan)
    app.state.store = store
    app.state.ingest = ingest

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state() -> JSONResponse:
        return JSONResponse(json.loads(json.dumps(store.state(len(clients)), default=str)))

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await websocket.accept()
        clients.add(websocket)
        try:
            with store.subscribe() as q:
                await websocket.send_text(json.dumps({"type": "state", **store.state(len(clients))}, default=str))
                while True:
                    await websocket.send_text(await q.get())
        except (WebSocketDisconnect, RuntimeError, OSError):
            pass  # the browser went away or fell too far behind; nothing upstream cares
        finally:
            clients.discard(websocket)

    return app


async def _call(fn: Any, *args: Any) -> None:
    fn(*args)


def _current_loop() -> asyncio.AbstractEventLoop | None:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def main(argv: list[str] | None = None) -> int:
    import uvicorn

    from orbit import log as orbit_log

    ap = argparse.ArgumentParser(description="Orbit monitoring display (telemetry sink + dashboard)")
    ap.add_argument("--port", type=int, default=8765, help="HTTP port for the dashboard")
    ap.add_argument("--telemetry-port", type=int, default=DEFAULTS.telemetry_port, help="UDP port the ground sends to")
    ap.add_argument("--host", default="0.0.0.0", help="bind address; 0.0.0.0 so the laptop is reachable on the LAN")
    a = ap.parse_args(argv)
    orbit_log.setup()
    print(f"display: http://{a.host}:{a.port}/  (telemetry UDP :{a.telemetry_port})", flush=True)
    server = uvicorn.Server(uvicorn.Config(create_app(a.telemetry_port), host=a.host, port=a.port, log_level="warning"))
    # uvicorn re-raises a captured SIGINT on exit, which asyncio.run turns into a KeyboardInterrupt inside the
    # loop and a traceback on every Ctrl-C. Own the signals instead: they just ask the server to stop.
    server.capture_signals = contextlib.nullcontext  # type: ignore[method-assign,assignment]

    async def serve() -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(NotImplementedError):
                loop.add_signal_handler(sig, lambda: setattr(server, "should_exit", True))
        await server.serve()

    asyncio.run(serve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
