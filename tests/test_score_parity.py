"""The firmware/golden scoring parity check has to run on a clone with nothing pre-built.

tools/check_score_parity.py is the artefact that proves the ESP32's scoring kernel is bit-exact
with the Python golden model on all seven intermediates. It used to stop at "build it first" and
return 2, which made the strongest claim in the repo the one nobody could reproduce. These tests
hold it to the opposite: given a tree with no host binary, it builds its own harness and passes.

The tool is run as a subprocess rather than imported and called. ``load_golden`` materialises the
golden modules from git into a temp dir and puts that dir on ``sys.path`` -- under pytest ``orbit``
is already in ``sys.modules``, so an in-process call would silently compare the firmware against
the working tree instead of against the committed revision, and prove nothing. ORBIT_SCORE_HOST
keeps both tests building into tmp_path, the way every other host harness here does, so nothing
in the tree is created or deleted underneath anyone.
"""

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tools.check_score_parity import build_host

ROOT = Path(__file__).resolve().parent.parent
TOOL = ROOT / "tools/check_score_parity.py"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="no g++ to build the host harness")


def test_a_missing_harness_is_built_and_a_stale_one_is_rebuilt(tmp_path):
    """Missing -> built (under -Werror, so a warning in the firmware header is a failure).
    Fresh -> left alone. Stale -> rebuilt, which is what makes keeping the binary in the tree
    safe: editing the firmware header cannot leave the check passing against the previous build.
    """
    exe = tmp_path / "score_host"
    assert build_host(exe) == exe
    assert exe.exists() and exe.stat().st_size > 0

    exe.touch()  # newer than every source, so nothing should be run
    fresh = exe.stat().st_mtime_ns
    assert build_host(exe).stat().st_mtime_ns == fresh

    os.utime(exe, ns=(0, 0))  # older than the firmware header it was built from
    assert build_host(exe).stat().st_mtime_ns != 0  # it really recompiled
    assert exe.stat().st_mtime_ns > fresh


def test_parity_passes_with_nothing_built_beforehand(tmp_path):
    """The whole point: the check runs from cold, builds its harness, and reports bit-exactness."""
    exe = tmp_path / "score_host"
    env = {**os.environ, "ORBIT_SCORE_HOST": str(exe)}
    r = subprocess.run([sys.executable, str(TOOL), "--n", "8"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    assert r.stdout.startswith("OK: 8 frames, all 7 intermediates bit-exact")
    assert exe.exists()  # it built the harness itself


def test_a_golden_ref_this_clone_does_not_have_is_named_not_raised(tmp_path):
    """Unknown is exit 2 with a readable reason, never a traceback that reads like a parity failure."""
    env = {**os.environ, "ORBIT_SCORE_HOST": str(tmp_path / "score_host"), "ORBIT_GOLDEN_REF": "no-such-ref"}
    r = subprocess.run([sys.executable, str(TOOL), "--n", "1"], capture_output=True, text=True, cwd=ROOT, env=env)
    assert r.returncode == 2
    assert "no-such-ref" in r.stderr and "Traceback" not in r.stderr
