"""Run every RTL check in one go: Verilator lint of the whole design (at the
board parameters and at the simulation parameters), the params header check,
and every cocotb testbench under tb/ (pass --slow for the 1024 x 240 run).

    uv run python tb/run_all.py [--slow]
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
RTL = ROOT / "rtl"
SOURCES = sorted(str(p) for p in RTL.glob("*.v") if p.name != "hello.v")

LINTS = [
    ["--top-module", "top_edge_node"],
    ["--top-module", "top_edge_node", "-GPPC=1"],
    ["--top-module", "top_edge_node", "-GCLK_HZ=1000000", "-GBAUD=100000"],
    ["--top-module", "edge_node_core", "-GCLK_HZ=1000000", "-GBAUD=100000", "-GI2C_HZ=25000",
     "-GHEARTBEAT_MS=50", "-GLINK_TIMEOUT_MS=120", "-GPOWER_PERIOD_MS=30"],
]


def main() -> int:
    slow = "--slow" in sys.argv
    for extra in LINTS:
        cmd = ["verilator", "--lint-only", "-Wall", f"-I{RTL}", *SOURCES, *extra]
        print("lint:", " ".join(extra))
        if subprocess.run(cmd, cwd=ROOT).returncode:
            return 1
    steps = [
        [sys.executable, str(ROOT / "tools" / "params_check.py")],
        [sys.executable, "-m", "pytest", str(ROOT / "tests" / "test_params_check.py"), "-q"],
        [sys.executable, "-m", "pytest", str(ROOT / "tb"), "-q", "-m", "slow" if slow else "not slow"],
    ]
    for cmd in steps:
        print("run:", " ".join(cmd[1:]))
        if subprocess.run(cmd, cwd=ROOT).returncode:
            return 1
    print("run_all: OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
