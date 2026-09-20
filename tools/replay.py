#!/usr/bin/env python3
"""Replay a runs/<run_id>.jsonl over the display's WebSocket, honouring t.

    python3 tools/replay.py runs/sample.jsonl                  # ws://localhost:8000/ws, real time
    python3 tools/replay.py runs/sample.jsonl --speed 4 --loop     # --pause N seconds between passes (default 10)
    python3 tools/replay.py runs/sample.jsonl --start 60       # jump in at t=60 s (state up to there applied silently)
    python3 tools/replay.py runs/sample.jsonl -v               # also log node_status / queue_window

Stdlib only, Python 3.9+: no uv, no pip, nothing to install on the display laptop.

    ws://HOST:PORT/ws       the events, one JSON text frame each, at (t - t0) / speed
    GET /api/state          accumulated current state, so a browser refresh recovers
    GET /                   display/live.html (same origin as the socket, so no CORS)
    GET /frames/<id>.png    thumbnails from --frames (default corpus/png) when present
    GET /fonts/...          any file that sits next to the page (the bundled Archivo)
    GET /runs/<run_id>.jsonl  the recorded run file, for the results sheet (see below)
    GET /api/runs           every .jsonl next to the file being replayed, newest first

The results sheet reads the recorded file, not live memory, exactly as the contract
says the stats screen should. The file being replayed is served as the ground
station's log would look right now: only the events sent so far in this pass, so a
run in progress has no run_end yet and the sheet fills in when the run ends. Any
other .jsonl in the same directory is served whole, so a finished run can be shown
while another one plays (?run=<run_id> on the page). A real ground station needs
the same two routes on the host that serves /ws, or a copy of the file next to it.

/api/state is the contract's own events, compacted: run_start, every frame_scored,
grant, frame_arrived, baseline_arrival and node_event so far, but only the LATEST
node_status and queue_window per node and the latest window_update, all in seq
order, plus "seq" and "t" of the last event and a "source" block describing the
replay. The page feeds them through the same reducer it uses for live events, so
live and replay stay one code path. A ground station that wants refresh-recovery
can serve the same shape from its own log without inventing a new schema.

Events are sent exactly as they are in the file. Lines that are not JSON objects
are skipped with a message; events without a numeric t go out right after the
previous one.
"""

from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import mimetypes
import re
import struct
import sys
import time
from pathlib import Path
from typing import Any

Event = dict[str, Any]

REPO = Path(__file__).resolve().parent.parent
HTML = REPO / "display" / "live.html"
FRAMES = REPO / "corpus" / "png"
GUID = b"258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
LATEST_PER_NODE = ("node_status", "queue_window")
LATEST = ("window_update",)
QUIET_TYPES = ("node_status", "queue_window")
LOOP_PAUSE_S = 10.0  # between passes when looping: long enough to read the finished run on both sheets
RUN_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:@-]*\.jsonl$")


# ---------------------------------------------------------------- state
class State:
    """Log compaction over the event stream: what a fresh page needs to catch up."""

    def __init__(self) -> None:
        self.reset()

    def reset(self) -> None:
        self.seq: int | None = None
        self.t: float | None = None
        self.n = 0
        self.run_start: tuple[int, Event] | None = None
        self.latest: dict[tuple[str, Any], tuple[int, Event]] = {}  # (type, node_id) -> (n, event)
        self.log: list[tuple[int, Event]] = []  # (n, event) for everything kept in full

    def apply(self, ev: Event) -> None:
        typ = ev.get("type")
        if typ == "run_start":
            self.reset()
        self.n += 1
        if isinstance(ev.get("seq"), int):
            self.seq = ev["seq"]
        if isinstance(ev.get("t"), (int, float)):
            self.t = ev["t"]
        if typ == "run_start":
            self.run_start = (self.n, ev)
        elif typ in LATEST_PER_NODE:
            self.latest[(typ, ev.get("node_id"))] = (self.n, ev)
        elif typ in LATEST:
            self.latest[(typ, None)] = (self.n, ev)
        else:
            self.log.append((self.n, ev))

    def events(self) -> list[Event]:
        items = ([self.run_start] if self.run_start else []) + self.log + list(self.latest.values())
        return [ev for _, ev in sorted(items, key=lambda x: x[0])]


# ---------------------------------------------------------------- websocket bits
def ws_frame(op: int, payload: bytes) -> bytes:
    n = len(payload)
    if n < 126:
        head = bytes([0x80 | op, n])
    elif n < 65536:
        head = bytes([0x80 | op, 126]) + struct.pack(">H", n)
    else:
        head = bytes([0x80 | op, 127]) + struct.pack(">Q", n)
    return head + payload


async def read_ws_frame(reader: asyncio.StreamReader) -> tuple[int, bytes] | None:
    """(opcode, payload) of one client frame, or None when the socket is gone."""
    try:
        b1, b2 = await reader.readexactly(2)
        op, masked, n = b1 & 0x0F, b2 & 0x80, b2 & 0x7F
        if n == 126:
            n = struct.unpack(">H", await reader.readexactly(2))[0]
        elif n == 127:
            n = struct.unpack(">Q", await reader.readexactly(8))[0]
        if n > (1 << 20):
            return None
        mask = await reader.readexactly(4) if masked else None
        payload = await reader.readexactly(n) if n else b""
    except (asyncio.IncompleteReadError, ConnectionError, OSError):
        return None
    if mask:
        payload = bytes(c ^ mask[i & 3] for i, c in enumerate(payload))
    return op, payload


# ---------------------------------------------------------------- describe (for the terminal)
def describe(ev: Event) -> str:
    g = ev.get
    typ = g("type")
    try:
        if typ == "run_start":
            nodes = ", ".join(f"{n.get('label')}({n.get('node_id')})" for n in g("nodes", []))
            w = g("window", {})
            return (
                f"run {g('run_id')} mode={g('mode')} nodes=[{nodes}] budget={w.get('budget_bytes')} B"
                f" duration={w.get('duration_s')} s queue_limit={g('queue_limit')}"
            )
        if typ == "frame_scored":
            return (
                f"node {g('node_id')} frame {g('frame_id')} score {g('score')} cloud {g('cloud_frac')}"
                f" queued={g('queued')} evicted={g('evicted_frame_id')} depth={g('queue_depth')}"
            )
        if typ == "grant":
            bids = " ".join(
                f"{b.get('node_id')}:{b.get('top_score')}{'' if b.get('ready') else '(not ready)'}"
                for b in g("bids", [])
            )
            return f"slot {g('slot_id')} -> node {g('node_id')}  {g('reason')}  bids {bids}"
        if typ == "frame_arrived":
            return (
                f"slot {g('slot_id')} node {g('node_id')} frame {g('frame_id')} score {g('score')}"
                f" {'USABLE' if g('usable') else 'not usable'} {g('bytes')} B in {g('duration_s')} s"
            )
        if typ == "baseline_arrival":
            return (
                f"FIFO would have sent node {g('node_id')} frame {g('frame_id')} score {g('score')}"
                f" {'usable' if g('usable') else 'not usable'}"
            )
        if typ == "window_update":
            return (
                f"used {g('used_bytes')}/{g('budget_bytes')} B  remaining {g('remaining_bytes')}"
                f"  {g('time_remaining_s')} s left  open={g('open')}"
            )
        if typ == "node_event":
            return f"node {g('node_id')} {str(g('level')).upper()} {g('message')}"
        if typ == "run_end":
            o, b, h = g("orbit", {}), g("baseline", {}), g("headline", {})
            return (
                f"orbit {o.get('usable_down')}/{o.get('frames_down')} usable,"
                f" baseline {b.get('usable_down')}/{b.get('frames_down')}, gain {h.get('gain')}"
            )
        if typ == "node_status":
            return (
                f"node {g('node_id')} depth {g('queue_depth')} top {g('top_frame_id')}@{g('top_score')}"
                f" busy={g('busy')} link={g('link_ok')}"
            )
        if typ == "queue_window":
            return f"node {g('node_id')} depth {g('depth')} top {[e.get('frame_id') for e in g('top', [])]}"
    except Exception:
        pass
    return json.dumps(ev)[:120]


# ---------------------------------------------------------------- server
class Replayer:
    def __init__(
        self,
        path: Path,
        speed: float,
        loop: bool,
        start: float,
        host: str,
        port: int,
        frames_dir: Path,
        html: Path,
        verbose: bool,
    ):
        self.path, self.speed, self.loop, self.start = path, speed, loop, start
        self.pause = LOOP_PAUSE_S
        self.host, self.port, self.frames_dir, self.html, self.verbose = host, port, frames_dir, html, verbose
        self.events = self.load(path)
        ts = [e["t"] for e in self.events if isinstance(e.get("t"), (int, float))]
        self.t_first: float
        self.t_last: float
        self.t_first, self.t_last = (min(ts), max(ts)) if ts else (0.0, 0.0)
        self.clients: set[asyncio.StreamWriter] = set()
        self.state = State()
        self.position = 0.0
        self.pass_no = 0
        self.playing = False
        self.sent = 0  # events already sent this pass: the "recorded file" so far
        self.run_id = next(
            (str(e["run_id"]) for e in self.events if e.get("type") == "run_start" and e.get("run_id") is not None),
            None,
        )

    @staticmethod
    def load(path: Path) -> list[Event]:
        events, bad = [], 0
        with open(path, encoding="utf-8") as f:
            for i, line in enumerate(f, 1):
                line = line.strip()
                if not line:
                    continue
                try:
                    ev = json.loads(line)
                except ValueError:
                    bad += 1
                    print(f"skip line {i}: not JSON", file=sys.stderr)
                    continue
                if not isinstance(ev, dict):
                    bad += 1
                    print(f"skip line {i}: not an object", file=sys.stderr)
                    continue
                events.append(ev)
        if not events:
            sys.exit(f"{path}: no events")
        if bad:
            print(f"{path}: skipped {bad} bad line(s)", file=sys.stderr)
        return events

    # -- playback
    async def play(self) -> None:
        self.playing = True
        while True:
            self.pass_no += 1
            self.state.reset()
            self.sent = 0
            i = 0
            if self.start > 0:
                while i < len(self.events) and (self.events[i].get("t") or 0) < self.start:
                    self.state.apply(self.events[i])
                    i += 1
                print(f"-- start at t={self.start} s: {i} events applied to state, not sent")
                self.sent = i
            base_t = max(self.start, self.t_first)
            wall0 = time.monotonic()
            for idx, ev in enumerate(self.events[i:], i):
                t = ev.get("t")
                if isinstance(t, (int, float)):
                    delay = (t - base_t) / self.speed - (time.monotonic() - wall0)
                    if delay > 0:
                        await asyncio.sleep(delay)
                    self.position = t
                self.state.apply(ev)
                self.sent = idx + 1
                await self.broadcast(ev)
                if self.verbose or ev.get("type") not in QUIET_TYPES:
                    print(
                        f"[t={self.position:8.3f}] #{ev.get('seq')!s:>4} {ev.get('type')!s:16s} {describe(ev)}",
                        flush=True,
                    )
            if not self.loop:
                print(f"-- end of {self.path.name}; holding the final state, Ctrl-C to quit", flush=True)
                self.playing = False
                return
            print(f"-- end of pass {self.pass_no}; looping in {self.pause:g} s", flush=True)
            await asyncio.sleep(self.pause)

    async def broadcast(self, ev: Event) -> None:
        if not self.clients:
            return
        data = ws_frame(0x1, json.dumps(ev, separators=(",", ":")).encode("utf-8"))
        for w in list(self.clients):
            try:
                w.write(data)
                await asyncio.wait_for(w.drain(), timeout=5)
            except Exception:
                self.drop(w)

    def drop(self, w: asyncio.StreamWriter) -> None:
        if w in self.clients:
            self.clients.discard(w)
            print(f"-- client left ({len(self.clients)} connected)", flush=True)
        with contextlib.suppress(Exception):
            w.close()

    def snapshot(self) -> dict[str, Any]:
        return {
            "source": {
                "kind": "replay",
                "file": self.path.name,
                "speed": self.speed,
                "loop": self.loop,
                "pass": self.pass_no,
                "playing": self.playing,
                "position_s": self.position,
                "duration_s": self.t_last,
                "events_total": len(self.events),
                "clients": len(self.clients),
            },
            "seq": self.state.seq,
            "t": self.state.t,
            "events": self.state.events(),
        }

    # -- connections
    async def handle(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        try:
            head = await asyncio.wait_for(reader.readuntil(b"\r\n\r\n"), timeout=10)
        except Exception:
            writer.close()
            return
        lines = head.decode("latin-1").split("\r\n")
        parts = lines[0].split()
        target = parts[1] if len(parts) >= 2 else "/"
        path = target.split("?", 1)[0]
        headers = {}
        for ln in lines[1:]:
            if ":" in ln:
                k, v = ln.split(":", 1)
                headers[k.strip().lower()] = v.strip()
        try:
            if path == "/ws" and "websocket" in headers.get("upgrade", "").lower():
                await self.websocket(reader, writer, headers)
            elif path == "/api/state":
                await self.respond(writer, 200, json.dumps(self.snapshot()).encode("utf-8"), "application/json")
            elif path in ("/", "/index.html", "/live.html"):
                await self.respond_file(
                    writer,
                    self.html,
                    fallback=b"<!doctype html><meta charset=utf-8><title>replay</title>"
                    b"<p style='font:16px system-ui;padding:2em'>replayer is up; <code>display/live.html</code> "
                    b"was not found next to it. The socket is at <code>/ws</code>, state at <code>/api/state</code>.",
                )
            elif path.startswith("/frames/"):
                name = path[len("/frames/") :]
                if (
                    "/" in name
                    or name.startswith(".")
                    or not name.replace("-", "").replace("_", "").replace(".", "").isalnum()
                ):
                    await self.respond(writer, 404, b"no", "text/plain")
                else:
                    await self.respond_file(writer, self.frames_dir / name, cache="max-age=3600")
            elif path.startswith("/runs/"):
                await self.run_file(writer, path[len("/runs/") :])
            elif path == "/api/runs":
                await self.respond(writer, 200, json.dumps(self.list_runs()).encode("utf-8"), "application/json")
            elif (static := self.static_file(path)) is not None:  # fonts/ and anything else next to the page
                await self.respond_file(writer, static, cache="max-age=3600")
            else:
                await self.respond(writer, 404, b"not found", "text/plain")
        except (ConnectionError, asyncio.CancelledError):
            pass
        finally:
            if writer not in self.clients:
                writer.close()

    # -- the recorded run files
    def recorded_so_far(self) -> list[Event]:
        """The file being replayed as the ground station's log would look right now."""
        return self.events[: self.sent]

    async def run_file(self, writer: asyncio.StreamWriter, name: str) -> None:
        """GET /runs/<name>.jsonl: the replaying file (by its own name or as <run_id>.jsonl)
        truncated to what has been sent; any other .jsonl in the same directory, whole."""
        if not RUN_NAME.match(name):
            await self.respond(writer, 404, b"no", "text/plain")
            return
        if name in (self.path.name, f"{self.run_id}.jsonl"):
            body = "".join(json.dumps(e, separators=(",", ":")) + "\n" for e in self.recorded_so_far())
            await self.respond(writer, 200, body.encode("utf-8"), "application/x-ndjson")
            return
        p = self.path.parent / name
        if p.is_file():
            await self.respond_file(writer, p)
        else:
            await self.respond(writer, 404, b"not found", "text/plain")

    def list_runs(self) -> list[dict[str, Any]]:
        """GET /api/runs: one entry per .jsonl next to the replaying file, newest first."""
        out = []
        for p in sorted(self.path.parent.glob("*.jsonl"), key=lambda q: q.stat().st_mtime, reverse=True):
            if not RUN_NAME.match(p.name):
                continue
            if p.resolve() == self.path.resolve():
                evs = self.recorded_so_far()
            else:
                evs = []
                try:
                    for line in p.read_text(encoding="utf-8").splitlines():
                        try:
                            ev = json.loads(line)
                        except ValueError:
                            continue
                        if isinstance(ev, dict):
                            evs.append(ev)
                except OSError:
                    continue
            start = next((e for e in evs if e.get("type") == "run_start"), None)
            end = next((e for e in reversed(evs) if e.get("type") == "run_end"), None)
            out.append(
                {
                    "file": p.name,
                    "run_id": start.get("run_id") if start else None,
                    "mode": start.get("mode") if start else None,
                    "events": len(evs),
                    "ended": end is not None,
                    "ended_t": end.get("t") if end else None,
                    "headline": end.get("headline") if end else None,
                    "playing": p.resolve() == self.path.resolve(),
                }
            )
        return out

    def static_file(self, path: str) -> Path | None:
        """A file in the page's own directory (display/fonts/..., say), or None. No dot
        segments, no hidden files, nothing outside that directory."""
        parts = [p for p in path.split("/") if p]
        if not parts or any(p in ("..", ".") or p.startswith(".") for p in parts):
            return None
        base = self.html.parent.resolve()
        target = (base / Path(*parts)).resolve()
        if base not in target.parents or not target.is_file():
            return None
        return target

    async def respond(
        self, writer: asyncio.StreamWriter, status: int, body: bytes, ctype: str, cache: str = "no-store"
    ) -> None:
        reason = {200: "OK", 404: "Not Found", 400: "Bad Request"}.get(status, "OK")
        head = (
            f"HTTP/1.1 {status} {reason}\r\nContent-Type: {ctype}\r\nContent-Length: {len(body)}\r\n"
            f"Access-Control-Allow-Origin: *\r\nCache-Control: {cache}\r\nConnection: close\r\n\r\n"
        )
        writer.write(head.encode("latin-1") + body)
        await writer.drain()

    async def respond_file(
        self, writer: asyncio.StreamWriter, p: Path, fallback: bytes | None = None, cache: str = "no-store"
    ) -> None:
        try:
            body = p.read_bytes()
        except OSError:
            if fallback is not None:
                await self.respond(writer, 200, fallback, "text/html; charset=utf-8")
            else:
                await self.respond(writer, 404, b"not found", "text/plain")
            return
        ctype = mimetypes.guess_type(p.name)[0] or {
            ".woff2": "font/woff2",
            ".woff": "font/woff",
            ".jsonl": "application/x-ndjson",
        }.get(p.suffix, "application/octet-stream")
        if ctype.startswith("text/"):
            ctype += "; charset=utf-8"
        await self.respond(writer, 200, body, ctype, cache)

    async def websocket(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter, headers: dict[str, str]
    ) -> None:
        key = headers.get("sec-websocket-key")
        if not key:
            await self.respond(writer, 400, b"missing Sec-WebSocket-Key", "text/plain")
            return
        accept = base64.b64encode(hashlib.sha1(key.encode("latin-1") + GUID).digest()).decode("ascii")
        writer.write(
            (
                "HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                f"Sec-WebSocket-Accept: {accept}\r\n\r\n"
            ).encode("latin-1")
        )
        await writer.drain()
        self.clients.add(writer)
        print(f"-- client connected ({len(self.clients)} connected); it should GET /api/state to catch up", flush=True)
        try:
            while True:
                fr = await read_ws_frame(reader)
                if fr is None:
                    break
                op, payload = fr
                if op == 0x8:  # close: echo the status code and stop
                    writer.write(ws_frame(0x8, payload[:2]))
                    await writer.drain()
                    break
                if op == 0x9:  # ping -> pong
                    writer.write(ws_frame(0xA, payload))
                    await writer.drain()
                # text, binary, pong: the display never has anything to say; ignored
        except Exception:
            pass
        finally:
            self.drop(writer)

    async def serve(self) -> None:
        server = await asyncio.start_server(self.handle, self.host, self.port)
        print(
            f"replay  {self.path}  {len(self.events)} events  t {self.t_first:.1f} -> {self.t_last:.1f} s"
            f"  speed {self.speed:g}x  loop {'on' if self.loop else 'off'}"
            + (f"  start {self.start:g} s" if self.start else "")
        )
        print(
            f"serving ws://{self.host}:{self.port}/ws   http://{self.host}:{self.port}/"
            f"  ({self.html.relative_to(REPO) if self.html.exists() else 'page missing'})"
            f"   /api/state   /frames/ -> {self.frames_dir.relative_to(REPO) if self.frames_dir.exists() else 'none'}"
            f"   /runs/{self.run_id or self.path.stem}.jsonl (as recorded so far)   /api/runs",
            flush=True,
        )
        player = asyncio.create_task(self.play())
        try:
            async with server:
                await server.serve_forever()
        finally:
            player.cancel()


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="Replay a run file over ws://HOST:PORT/ws, honouring t.")
    ap.add_argument("file", nargs="?", default=str(REPO / "runs" / "sample.jsonl"))
    ap.add_argument("--speed", type=float, default=1.0, help="playback speed multiplier (default 1)")
    ap.add_argument("--loop", action="store_true", help="start over when the file ends")
    ap.add_argument(
        "--pause",
        type=float,
        default=LOOP_PAUSE_S,
        help=f"seconds to hold the finished run before looping (default {LOOP_PAUSE_S:g})",
    )
    ap.add_argument(
        "--start", type=float, default=0.0, help="begin at this t; earlier events are applied to state silently"
    )
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--frames", default=str(FRAMES), help="directory of <id>.png thumbnails served at /frames/")
    ap.add_argument("--html", default=str(HTML), help="page served at /")
    ap.add_argument("-v", "--verbose", action="store_true", help="also log node_status and queue_window")
    a = ap.parse_args(argv)
    if a.speed <= 0:
        ap.error("--speed must be > 0")
    r = Replayer(Path(a.file), a.speed, a.loop, a.start, a.host, a.port, Path(a.frames), Path(a.html), a.verbose)
    r.pause = max(0.0, a.pause)
    try:
        asyncio.run(r.serve())
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
