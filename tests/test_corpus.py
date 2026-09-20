"""Offline checks on corpus/ and the orbit.corpus loader. No network."""

import subprocess
import sys
from pathlib import Path

import numpy as np
import pytest

import orbit.corpus as oc


@pytest.fixture(scope="module")
def c():
    return oc.load()


def test_shapes_and_ids(c):
    n = len(c.frames)
    assert n >= 100
    assert c.frames.shape == (n, 128, 128) and c.frames.dtype == np.uint8
    assert c.ids.dtype == np.uint16 and list(c.ids) == list(range(n))
    assert [f["id"] for f in c.manifest["frames"]] == list(range(n))
    assert len(list(oc.ROOT.glob("png/*.png"))) == n
    assert not oc.SYNTHETIC and provenance_ok()


def provenance_ok():
    p = oc.provenance()
    return p["frames"] > 0 and "GIBS" in p["licence"] and p["layer"]


def test_every_frame_has_reference_in_its_scene(c):
    for f in c.manifest["frames"]:
        r = c.reference_for(f["id"])
        assert c.scene_of(r) == f["scene"]
        assert c.by_id(r).shape == (128, 128)
        assert c.png_path(f["id"]).exists()


def test_sequence(c):
    n = len(c.frames)
    s0, s1 = c.sequence(0), c.sequence(1)
    assert s0 == c.sequence(0) and sorted(s0) == list(range(n)) and sorted(s1) == list(range(n))
    assert s0 != s1
    for s in (s0, s1):
        seen = set()
        for fid in s:
            assert c.reference_for(fid) == fid or c.reference_for(fid) in seen
            seen.add(fid)
    assert len(c.sequence(0, n=5)) == 5 and len(c.sequence(0, n=n + 7)) == n + 7


def test_loader_imports_no_network_modules():
    # A subprocess: in-process sys.modules already holds urllib.request via httpx/TestClient.
    code = (
        "import sys, orbit.corpus; oc = orbit.corpus.load(); "
        "bad = {'urllib.request', 'requests', 'orbit.corpus.fetch'} & set(sys.modules); assert not bad, bad"
    )
    subprocess.run([sys.executable, "-c", code], check=True, cwd=Path(__file__).resolve().parents[1])
