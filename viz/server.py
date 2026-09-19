"""FastAPI + WebSocket view over the orchestrator.

    uv run python -m viz.server --scenario nominal      # http://localhost:8000

WebSocket /ws  (10 Hz state stream)
  server -> client   {"type":"snapshot", ...Orchestrator.snapshot()}      every SNAPSHOT_MS and after each command
                     {"type":"event","event":"capture"|"scored"|"slot"|"window_open"|"window_close"|"pass"|
                                     "status"|"config"|"ref"|"pass_start"|"done", "t_ms":..., ...payload}
  client -> server   {"cmd":"play"|"pause"|"step"}
                     {"cmd":"speed","value":0.25..16}   {"cmd":"starvation","value":n}
                     {"cmd":"scenario","value":name}    -> restarts the orchestrator on fresh nodes
                     {"cmd":"sim_nodes","value":n}      -> restarts with n extra simulated satellites
                     {"cmd":"scaling_sweep"}            -> headless runs for N = 2..MAX_SWEEP simulated satellites
REST mirrors of the controls (JSON body {"value": ...} where a value is needed):
  POST /api/<cmd>   GET /api/state -> the same snapshot the socket carries
  GET  /corpus/<id>.png -> the frame the node scored (the ground holds the corpus; §6)
The browser is a view: every number it shows arrives on this socket.

Every snapshot also carries the two off-board result files, so the efficiency
panel flips from TBD to measured by itself:
  "vivado": tools.vivado_reports.load_summary(vivado/reports/summary.json) or all-TBD;
  "bench":  newest bench/results/<stamp>.json (orbit.bench.report.BenchReport.save) or null.
Paths: create_app(vivado_summary=..., bench_dir=...) or ORBIT_VIVADO_SUMMARY / ORBIT_BENCH_DIR.
"""
from __future__ import annotations

import argparse
import asyncio
import contextlib
import glob
import json
import os
from pathlib import Path

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from orbit import corpus as C, params
from orbit.orchestrator import scenarios
from tools import vivado_reports

STATIC = Path(__file__).parent / "static"
REPO = Path(__file__).resolve().parent.parent
VIVADO_SUMMARY = str(REPO / "vivado" / "reports" / "summary.json")
BENCH_DIR = str(REPO / "bench" / "results")
SNAPSHOT_MS = 100   # 10 Hz
COMMANDS = ("scenario", "speed", "play", "pause", "step", "starvation", "sim_nodes", "scaling_sweep")
MAX_SWEEP = 12
PARAMS_SHOWN = ("FRAME_W", "FRAME_H", "FRAME_BYTES", "PIXELS_PER_CYCLE", "QUEUE_DEPTH", "CLK_HZ", "BAUD",
                "STARVATION_N", "WINDOW_DURATION_S", "LINK_RATE_BPS")


def load_bench(directory: str) -> dict | None:
    """Newest bench/results/<stamp>.json (stamps are YYYYMMDD-HHMMSS, so lexical
    order is chronological); None when there is no run yet."""
    paths = sorted(glob.glob(os.path.join(directory, "*.json")))
    if not paths:
        return None
    try:
        with open(paths[-1], encoding="utf-8") as f:
            entries = json.load(f)
    except (OSError, ValueError):          # half-written file: absent, not a dead ticker
        return None
    return {"stamp": Path(paths[-1]).stem, "path": paths[-1], "entries": entries}


def scaling_sweep(base_nodes: list[str], n_max: int = MAX_SWEEP, passes: int = 2, seed: int = 0) -> list[dict]:
    """Delivered value vs number of satellites, all simulated, same window each: where demand
    (N x frames per pass) crosses capacity (slots per pass) the curve flattens. Headless, virtual clock."""
    out = []
    for n in range(2, n_max + 1):
        nodes = [f"sim://{k}" for k in range(n)]
        o = scenarios.build("scaling", nodes=nodes, passes=passes, seed=seed, virtual=True)
        asyncio.run(o.run())
        o.close()
        out.append(dict(n=n, physical=0, simulated=n, slots=len(o.slots), filtered=o.value_filtered, fifo=o.value_fifo,
                        captured=sum(x.captures for x in o.nodes), evicted=sum(x.mirror.evicted for x in o.nodes),
                        demand_frames=n * o.frames_per_pass * passes, capacity_frames=o.window.slots_total * passes,
                        starvation_switches=o.arbiter.starvation_switches))
    return out


class Session:
    def __init__(self, scenario: str, nodes: list[str], speed: float, virtual: bool = False, paused: bool = False,
                 seed: int = 0, vivado_summary: str | None = None, bench_dir: str | None = None):
        self.base_nodes, self.speed, self.virtual, self.paused, self.seed = list(nodes), speed, virtual, paused, seed
        self.sim_extra = 0
        self.vivado_summary = vivado_summary or os.environ.get("ORBIT_VIVADO_SUMMARY", VIVADO_SUMMARY)
        self.bench_dir = bench_dir or os.environ.get("ORBIT_BENCH_DIR", BENCH_DIR)
        self.scenario = scenario
        self.orch = None
        self.tasks: list[asyncio.Task] = []
        self.clients: set[WebSocket] = set()
        self.scaling: dict = dict(state="idle", rows=[])
        self.provenance = C.provenance()

    @property
    def nodes(self) -> list[str]:
        k0 = len(self.base_nodes)
        return self.base_nodes + [f"sim://{k}" for k in range(k0, k0 + self.sim_extra)]

    async def start(self, scenario: str) -> None:
        await self.stop()
        self.scenario = scenario
        self.orch = scenarios.build(scenario, nodes=self.nodes, speed=self.speed, virtual=self.virtual, seed=self.seed)
        if self.paused:
            self.orch.pause()
        self.tasks = [asyncio.create_task(self.orch.run()), asyncio.create_task(self._forward()),
                      asyncio.create_task(self._ticker())]

    async def stop(self) -> None:
        for t in self.tasks:
            t.cancel()
        for t in self.tasks:
            with contextlib.suppress(asyncio.CancelledError):
                await t
        self.tasks = []
        if self.orch:
            self.orch.close()

    def snapshot(self) -> dict:
        s = self.orch.snapshot()
        s["scenarios"] = scenarios.SCENARIOS
        s["params"] = {k: getattr(params, k) for k in PARAMS_SHOWN}
        s["physical_nodes"] = [n for n in self.nodes if not n.startswith("sim://")]
        s["sim_extra"] = self.sim_extra
        s["scaling"] = self.scaling
        try:
            s["vivado"] = vivado_reports.load_summary(self.vivado_summary)   # all-TBD, measured=false when absent
        except (OSError, ValueError):
            s["vivado"] = vivado_reports.empty_summary()
        s["bench"] = load_bench(self.bench_dir)
        return s

    async def _forward(self) -> None:
        q = self.orch.subscribe()
        while True:
            await self.broadcast(await q.get())

    async def _ticker(self) -> None:
        while True:
            await asyncio.sleep(SNAPSHOT_MS / 1000)
            await self.broadcast(self.snapshot())

    async def broadcast(self, msg: dict) -> None:
        data = json.dumps(msg)
        for ws in list(self.clients):
            try:
                await ws.send_text(data)
            except Exception:
                self.clients.discard(ws)

    async def _sweep(self) -> None:
        self.scaling = dict(state="running", rows=[])
        try:
            rows = await asyncio.to_thread(scaling_sweep, self.base_nodes)
            self.scaling = dict(state="done", rows=rows, note="all satellites simulated on the golden model; "
                                "same scaled window as the live run")
        except Exception as e:                       # surfaced on the panel, never a dead ticker
            self.scaling = dict(state="error", rows=[], note=str(e))

    async def handle(self, cmd: dict) -> None:
        o, c, v = self.orch, cmd.get("cmd"), cmd.get("value")
        if c == "play":
            o.play()
        elif c == "pause":
            o.pause()
        elif c == "step":
            o.step()
        elif c == "speed":
            o.set_speed(float(v))
            self.speed = o.speed
        elif c == "starvation":
            o.set_starvation(int(v))
        elif c == "scenario" and v in scenarios.SCENARIOS:
            await self.start(v)
        elif c == "sim_nodes":
            self.sim_extra = max(0, min(MAX_SWEEP, int(v)))
            await self.start(self.scenario)
        elif c == "scaling_sweep" and self.scaling["state"] != "running":
            asyncio.create_task(self._sweep())


def create_app(scenario: str = "nominal", nodes: list[str] = params.NODES, speed: float = 2.0, virtual: bool = False,
               paused: bool = False, seed: int = 0, vivado_summary: str | None = None,
               bench_dir: str | None = None) -> FastAPI:
    session = Session(scenario, nodes, speed, virtual, paused, seed, vivado_summary, bench_dir)

    @contextlib.asynccontextmanager
    async def lifespan(app):
        await session.start(scenario)
        yield
        await session.stop()

    app = FastAPI(lifespan=lifespan)
    app.state.session = session
    app.mount("/static", StaticFiles(directory=STATIC), name="static")
    app.mount("/corpus", StaticFiles(directory=C.ROOT / "png"), name="corpus")

    @app.get("/")
    async def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/state")
    async def state():
        return JSONResponse(session.snapshot())

    @app.post("/api/{cmd}")
    async def control(cmd: str, body: dict = Body(default={})):
        if cmd not in COMMANDS:
            return JSONResponse({"error": f"unknown command {cmd}"}, status_code=404)
        await session.handle({"cmd": cmd, "value": body.get("value")})
        return JSONResponse(session.snapshot())

    @app.websocket("/ws")
    async def ws(websocket: WebSocket):
        await websocket.accept()
        session.clients.add(websocket)
        try:
            await websocket.send_text(json.dumps(session.snapshot()))
            while True:
                cmd = json.loads(await websocket.receive_text())
                await session.handle(cmd)
                await websocket.send_text(json.dumps(session.snapshot()))
        except WebSocketDisconnect:
            pass
        finally:
            session.clients.discard(websocket)

    return app


def main(argv=None) -> int:
    import uvicorn
    ap = argparse.ArgumentParser()
    ap.add_argument("--scenario", default="nominal", choices=sorted(scenarios.SCENARIOS))
    ap.add_argument("--nodes", nargs="+", default=params.NODES)
    ap.add_argument("--speed", type=float, default=2.0)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args(argv)
    uvicorn.run(create_app(args.scenario, args.nodes, args.speed, seed=args.seed), host="127.0.0.1", port=args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
