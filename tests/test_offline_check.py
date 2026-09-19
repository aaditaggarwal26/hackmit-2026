"""The offline proof, in-process: tools/offline_check.py's three checks each pass.
No subprocess, so it is part of `uv run pytest` and a regression (a new HTTP call
reachable without --live, a lost vendor file) fails the suite."""
import socket

import pytest

from tools import offline_check as oc


def test_static_import_graph_has_no_network_outside_live_path():
    ok, lines = oc.check_static()
    assert ok, "\n".join(lines)
    assert any("allowed: orbit/corpus/fetch.py" in l and "fetch_live" in l for l in lines), lines
    assert any("not imported by the demo" in l for l in lines), lines


def test_guard_blocks_non_loopback_and_restores():
    real = socket.socket
    with oc.no_network():
        with pytest.raises(oc.NetworkBlocked):
            socket.create_connection(("gibs.earthdata.nasa.gov", 443), timeout=1)
        with pytest.raises(oc.NetworkBlocked):
            socket.getaddrinfo("gibs.earthdata.nasa.gov", 443)
        assert oc._is_loopback(("127.0.0.1", 8000)) and oc._is_loopback("/tmp/x.sock") and not oc._is_loopback(("1.1.1.1", 53))
    assert socket.socket is real


def test_dynamic_orchestrator_and_server_run_with_network_blocked():
    ok, lines = oc.check_dynamic(segments=2)
    assert ok, "\n".join(lines)
    assert any(l.startswith("guard armed") for l in lines), lines
    assert any("scenario=nominal" in l for l in lines), lines
    assert any("WS /ws -> first message type=snapshot" in l for l in lines), lines


def test_assets_local_and_corpus_thumbnails_present():
    ok, lines = oc.check_assets()
    assert ok, "\n".join(lines)


def test_scan_file_catches_network_outside_live_path(tmp_path):
    src = '''
import requests
def pull(live=False):
    if live:
        return fetch_live("x")
    return fetch_live("y")
def other():
    return socket.create_connection(("gibs.earthdata.nasa.gov", 443))
'''
    allowed, bad, n = oc.scan_file(tmp_path / "rogue.py", __import__("ast").parse(src))
    assert not allowed and n == 2
    assert any("requests in <module>" in b for b in bad)
    assert any("fetch_live() called outside" in b for b in bad)
    assert any("socket.create_connection() in other" in b for b in bad)
