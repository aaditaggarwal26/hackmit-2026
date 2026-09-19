"""rtl/orbit_params.vh must equal orbit/params.py (tools/params_check.py)."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "tools"))
import params_check  # noqa: E402


def test_rtl_params_match_python():
    assert params_check.check() == []
