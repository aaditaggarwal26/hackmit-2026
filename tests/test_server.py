"""viz/server.py over sim nodes with a virtual clock: snapshot shape, controls, corpus route,
efficiency panel flips from TBD to measured when the report files exist."""
import json
import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from orbit import params  # noqa: E402
from orbit.bench.report import BenchReport  # noqa: E402
from tools import vivado_reports as vr  # noqa: E402
from viz.server import create_app  # noqa: E402

FIX = Path(__file__).parent / "fixtures" / "vivado"


def recv_until(ws, pred, limit=400):
    for _ in range(limit):
        m = ws.receive_json()
        if pred(m):
            return m
    raise AssertionError("condition not met")


def test_ws_snapshot_controls_and_run():
    app = create_app("nominal", nodes=["sim://0", "sim://1"], speed=8.0, virtual=True, paused=True)
    with TestClient(app) as client, client.websocket_connect("/ws") as ws:
        s = ws.receive_json()
        assert s["type"] == "snapshot" and s["scenario"] == "nominal" and s["paused"] and s["pass_idx"] <= 0
        assert [n["path"] for n in s["nodes"]] == ["sim://0", "sim://1"] and "starvation" in s["scenarios"]
        assert s["window"]["scaled"] and s["provenance"]["synthetic"] is False and s["params"]["BAUD"] == params.BAUD
        assert s["vivado"]["measured"] is False and s["bench"] is None
        ws.send_json({"cmd": "starvation", "value": 2})
        s = recv_until(ws, lambda m: m["type"] == "snapshot" and m["starvation_n"] == 2)
        ws.send_json({"cmd": "play"})
        s = recv_until(ws, lambda m: m["type"] == "snapshot" and m["done"], limit=3000)
        assert s["n_slots"] == s["window"]["slots_total"] * s["n_passes"]
        assert s["value"]["filtered"] > 0 and all(n["mismatches"] == 0 for n in s["nodes"])
        assert s["nodes"][0]["last_capture"]["frame_id"] is not None and s["nodes"][0]["sent"]
        ws.send_json({"cmd": "scenario", "value": "lead_change"})
        s = recv_until(ws, lambda m: m["type"] == "snapshot" and m["scenario"] == "lead_change")
        assert s["pass_idx"] <= 0


def test_rest_controls_corpus_route_and_sim_nodes():
    app = create_app("nominal", virtual=True, paused=True)
    with TestClient(app) as client:
        assert "<title>" in client.get("/").text
        s = client.get("/api/state").json()
        assert s["type"] == "snapshot"
        fid = s["nodes"][0]["ref_id"] if s["nodes"][0]["ref_id"] is not None else 0
        r = client.get(f"/corpus/{fid:04d}.png")
        assert r.status_code == 200 and r.content[:4] == b"\x89PNG"
        assert client.post("/api/speed", json={"value": 4}).json()["speed"] == 4.0
        s = client.post("/api/sim_nodes", json={"value": 3}).json()
        assert [n["path"] for n in s["nodes"]] == ["sim://0", "sim://1", "sim://2", "sim://3", "sim://4"]
        assert all(n["simulated"] for n in s["nodes"]) and s["sim_extra"] == 3
        assert client.post("/api/nope").status_code == 404


def test_efficiency_panel_reads_reports(tmp_path):
    import shutil
    shutil.copytree(FIX, tmp_path / "reports")
    summary, _ = vr.write_summary(str(tmp_path / "reports"))
    bench = tmp_path / "bench"
    rep = BenchReport()
    rep.add("jetson-cpu-numpy", frames=12000, seconds=10.0, sampler=dict(label="device-level", mean_watts=5.0, joules=50.0, source="ina3221"))
    rep.save(str(bench), stamp="20260914-120000")
    app = create_app("nominal", virtual=True, paused=True, vivado_summary=str(summary), bench_dir=str(bench))
    with TestClient(app) as client:
        s = client.get("/api/state").json()
        assert s["vivado"]["measured"] is True
        assert s["bench"]["stamp"] == "20260914-120000" and s["bench"]["entries"][0]["platform"] == "jetson-cpu-numpy"
