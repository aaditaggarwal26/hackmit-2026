"""pytest glue for the cocotb testbenches: make the repo root importable and
register the `slow` marker (the full 1024 x 240 run takes minutes under
Verilator; run it with `-m slow`, skip it with `-m "not slow"`)."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def pytest_configure(config):
    config.addinivalue_line("markers", "slow: full-size RTL regression (minutes under Verilator)")
