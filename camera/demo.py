#!/usr/bin/env python3
"""One satellite, one photo at a time: the scoring demo.

    uv run python camera/demo.py            # then open http://localhost:8100/

Stands alone. It is not the ground station, it does not speak docs/event_stream.md,
and it has nothing to do with display/live.html: this is the half of the system
that happens on the board, shown by itself. A photo comes in, the kernel scores
it on cloud cover, detail and change, and the priority queue decides whether it
is worth keeping. Nothing is ever downlinked here.

The camera is the browser's. The page asks Chrome for the webcam, shows a live
preview, and when you press the button it takes one frame, reduces it to the
128x128 8-bit gray the board works in, and posts those 16384 bytes here. So the
camera permission is Chrome's normal one, there is no opencv, and no terminal.

The scoring is not the browser's. Every number on the page is computed here by
orbit.golden.score, the same kernel tools/check_score_parity.py holds the ESP32
firmware's C to, bit for bit, on all seven intermediates -- and the queue is
orbit.golden.queue.PriorityQueue at its real depth. The page only draws what this
file returns.

No camera (or no permission) is not a failure: "Use a sample photo" pulls a
frame from the committed corpus and scores it the same way.

    GET  /                the page
    GET  /api/config      the weights, thresholds and rules in force
    POST /api/score       16384 raw bytes -> the score, the maps and the queue
    POST /api/reset       empty the queue, forget the reference frame
    GET  /api/sample      16384 raw bytes from the corpus, for the fallback
"""

from __future__ import annotations

import argparse
import base64
import contextlib
import json
import struct
import sys
import threading
import zlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))

from orbit import config  # noqa: E402
from orbit.golden.queue import NO_FRAME, PriorityQueue  # noqa: E402
from orbit.golden.score import DEFAULT_CONFIG, Config, display, score_frame  # noqa: E402

H, W, NPIX = config.FRAME_H, config.FRAME_W, config.FRAME_BYTES
QUEUE_LIMIT = 8  # the board's runtime limit; small so eviction is visible
USABLE_MAX = 0.35  # cloud_frac above this and the photo is not worth the bandwidth


# ---------------------------------------------------------------- png
def png(a: np.ndarray) -> str:
    """HxW gray or HxWx3 rgb -> a data: URI. Stdlib, so the demo needs no image library."""
    a = np.ascontiguousarray(a, dtype=np.uint8)
    colour = 2 if a.ndim == 3 else 0
    rows = a.reshape(a.shape[0], -1)

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return struct.pack(">I", len(data)) + body + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)

    raw = b"".join(b"\x00" + bytes(r) for r in rows)
    blob = (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", a.shape[1], a.shape[0], 8, colour, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )
    return "data:image/png;base64," + base64.b64encode(blob).decode("ascii")


def paint(frame: np.ndarray, mask: np.ndarray, rgb: tuple[int, int, int]) -> np.ndarray:
    """The frame in gray with the masked pixels in one colour: what the term counted."""
    out = np.repeat(frame[:, :, None], 3, axis=2).astype(np.uint16)
    out = (out * 55 // 100) + 40  # dim the background so the mask reads
    out[mask] = rgb
    return out.astype(np.uint8)


def edge_map(frame: np.ndarray) -> np.ndarray:
    """The sharpness term as a picture: |Gx|+|Gy| per pixel, the same taps score.py sums."""
    p = frame.astype(np.int32)
    gx = (p[:-2, 2:] - p[:-2, :-2]) + ((p[1:-1, 2:] - p[1:-1, :-2]) << 1) + (p[2:, 2:] - p[2:, :-2])
    gy = (p[2:, :-2] - p[:-2, :-2]) + ((p[2:, 1:-1] - p[:-2, 1:-1]) << 1) + (p[2:, 2:] - p[:-2, 2:])
    mag = np.abs(gx) + np.abs(gy)
    out = np.zeros((H, W), np.uint8)
    out[1:-1, 1:-1] = np.clip(mag // 4, 0, 255).astype(np.uint8)
    return out


# ---------------------------------------------------------------- the node
class Session:
    """One edge node's working state: the last photo it saw, and what it is holding."""

    def __init__(self, limit: int = QUEUE_LIMIT, cfg: Config = DEFAULT_CONFIG):
        self.lock = threading.Lock()
        self.cfg, self.limit = cfg, limit
        self.reset()

    def reset(self) -> None:
        self.queue = PriorityQueue(self.limit)
        self.ref: np.ndarray | None = None
        self.ref_id: int | None = None
        self.next_id = 1
        self.shots: dict[int, dict] = {}

    def score(self, frame: np.ndarray) -> dict:
        cfg = self.cfg
        first = self.ref is None
        ref, ref_id = (frame, None) if first else (self.ref, self.ref_id)
        s = score_frame(frame, ref, cfg)
        fid, self.next_id = self.next_id, self.next_id + 1

        lost = self.queue.insert(s.score, fid)
        rejected = lost == fid  # the queue was full of better photos
        evicted = None if lost in (NO_FRAME, fid) else lost

        clear_part = (cfg.w_clear * s.clear) >> 16  # the three contributions sum to the score
        sharp_part = (cfg.w_sharp * s.sharp) >> 16
        cloud_frac = s.cloud_px / NPIX
        shot = dict(
            frame_id=fid,
            score=s.score,
            display=round(display(s.score), 1),
            cloud_frac=round(cloud_frac, 4),
            usable=cloud_frac <= USABLE_MAX,
            thumb=png(frame),
        )
        self.shots[fid] = shot
        self.ref, self.ref_id = frame, fid
        return dict(
            **{k: v for k, v in shot.items() if k != "thumb"},
            first=first,
            ref_id=ref_id,  # the photo this one was compared against
            terms=dict(clear=s.clear, sharp=s.sharp, change=s.change),
            parts=dict(clear=clear_part, sharp=sharp_part, change=s.score - clear_part - sharp_part),
            counts=dict(cloud_px=s.cloud_px, changed_px=s.changed_px, sobel_sum=s.sobel_sum, pixels=NPIX),
            queued=not rejected,
            rejected=rejected,
            evicted_frame_id=evicted,
            maps=dict(
                frame=shot["thumb"],
                cloud=png(paint(frame, frame > cfg.cloud_thr, (192, 39, 31))),
                edges=png(edge_map(frame)),
                change=png(
                    paint(frame, np.abs(frame.astype(np.int16) - ref.astype(np.int16)) > cfg.change_thr, (27, 84, 200))
                ),
            ),
            **self.state(),
        )

    def state(self) -> dict:
        return dict(
            depth=len(self.queue),
            limit=self.limit,
            evicted_total=self.queue.evicted,
            queue=[dict(self.shots[i], rank=k) for k, (_, i) in enumerate(self.queue.cells)],
        )

    def config(self) -> dict:
        c = self.cfg
        return dict(
            w_clear=c.w_clear,
            w_sharp=c.w_sharp,
            w_change=c.w_change,
            cloud_thr=c.cloud_thr,
            change_thr=c.change_thr,
            sharp_shift=c.sharp_shift,
            queue_limit=self.limit,
            frame=dict(w=W, h=H, bytes=NPIX),
            usable_rule=dict(metric="cloud_frac", max=USABLE_MAX),
            kernel="orbit/golden/score.py",
            **self.state(),
        )


# ---------------------------------------------------------------- server
class Handler(BaseHTTPRequestHandler):
    server_version = "orbit-camera"
    session: Session
    corpus_error: str | None = None

    def log_message(self, *a):  # one line per photo is plenty; the rest is noise
        pass

    def reply(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        with contextlib.suppress(BrokenPipeError, ConnectionResetError):
            self.wfile.write(body)

    def json(self, obj, code: int = 200) -> None:
        self.reply(code, json.dumps(obj).encode("utf-8"), "application/json")

    def file(self, p: Path, ctype: str) -> None:
        try:
            self.reply(200, p.read_bytes(), ctype)
        except OSError:
            self.reply(404, b"not found", "text/plain; charset=utf-8")

    def do_GET(self) -> None:
        path = self.path.split("?", 1)[0]
        if path in ("/", "/index.html"):
            self.file(HERE / "index.html", "text/html; charset=utf-8")
        elif path == "/api/config":
            self.json(self.session.config())
        elif path == "/api/sample":
            self.sample()
        elif path.startswith("/fonts/") and (HERE / "fonts" / Path(path).name).is_file():
            self.file(HERE / "fonts" / Path(path).name, "font/woff2")
        else:
            self.reply(404, b"not found", "text/plain; charset=utf-8")

    def do_POST(self) -> None:
        path = self.path.split("?", 1)[0]
        if path == "/api/reset":
            with self.session.lock:
                self.session.reset()
            print("-- queue emptied", flush=True)
            self.json(self.session.config())
        elif path == "/api/score":
            self.score()
        else:
            self.reply(404, b"not found", "text/plain; charset=utf-8")

    def score(self) -> None:
        n = int(self.headers.get("content-length") or 0)
        if n != NPIX:
            self.json({"error": f"expected {NPIX} bytes of 8-bit gray, got {n}"}, 400)
            return
        try:
            frame = np.frombuffer(self.rfile.read(n), dtype=np.uint8).reshape(H, W)
            with self.session.lock:
                out = self.session.score(frame)
        except Exception as e:  # a demo that crashes on a bad frame is no demo
            self.json({"error": f"{type(e).__name__}: {e}"}, 500)
            return
        print(
            f"photo #{out['frame_id']:<3d} score {out['display']:5.1f}/100  cloud {out['cloud_frac'] * 100:5.1f}%  "
            f"{'usable' if out['usable'] else 'NOT USABLE'}  "
            f"{'queued' if out['queued'] else 'rejected, queue full'}"
            f"{f', pushed out #{out["evicted_frame_id"]}' if out['evicted_frame_id'] else ''}",
            flush=True,
        )
        self.json(out)

    def sample(self) -> None:
        """A corpus frame, for showing the demo with no camera at all."""
        try:
            from orbit import corpus as C

            c = C.load()
            ids = [int(i) for i in c.ids]
            fid = ids[self.session.next_id * 7 % len(ids)]
            self.reply(200, bytes(np.ascontiguousarray(c.by_id(fid), dtype=np.uint8)), "application/octet-stream")
        except Exception as e:
            self.json({"error": f"no corpus to fall back on: {e}"}, 503)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8100)
    ap.add_argument("--queue", type=int, default=QUEUE_LIMIT, help=f"photos the node can hold (default {QUEUE_LIMIT})")
    a = ap.parse_args(argv)

    Handler.session = Session(max(1, min(a.queue, config.QUEUE_DEPTH)))
    try:
        httpd = ThreadingHTTPServer((a.host, a.port), Handler)
    except OSError as e:  # something else has the port: say which, not a traceback
        print(
            f"! port {a.port} is busy ({e.strerror or e}). Something is already serving there - the display\n"
            f"  replayer uses 8000. Pick another: uv run python camera/demo.py --port {a.port + 1}",
            file=sys.stderr,
        )
        return 1
    print(
        f"orbit camera demo   http://{a.host}:{a.port}/\n"
        f"scoring with orbit/golden/score.py, queue depth {Handler.session.limit}\n"
        f"Its own process on its own port: the display and its replayer are untouched by this.\n"
        f"Chrome will ask for the camera the first time you open the page. Ctrl-C to stop.",
        flush=True,
    )
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
