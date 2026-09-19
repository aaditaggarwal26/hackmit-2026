"""VerilatorNode: the compiled RTL as a subprocess, UART over a pty, behind
the same NodeTransport interface. Building on demand keeps one command
(`--nodes verilator://0`) the only thing a user has to type."""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from orbit.protocol.transport import HardwareNode

SIM_DIR = Path(__file__).resolve().parents[2] / "sim"
SIM_CLK_HZ = 2_000_000  # simulated clock; near the Mcycles/s Verilator achieves so sim-ms ~ wall-ms (see sim/Makefile)
SIM_BAUD = SIM_CLK_HZ // 10   # pty has no physical baud; this only sets cycles per bit inside the sim


class VerilatorNode(HardwareNode):
    def __init__(self, node_id: int, baud: int | None = None):
        # `baud` is the wire baud the caller uses for real boards; a pty has none. The
        # harness must be told the baud the RTL was BUILT with (sim/Makefile BAUD),
        # otherwise it bit-bangs at the wrong rate and every frame is garbage.
        binary = SIM_DIR / "obj_dir" / "Vtop_edge_node"
        if not binary.exists():
            subprocess.run(["make", "-C", str(SIM_DIR), f"BAUD={SIM_BAUD}", f"CLK_HZ={SIM_CLK_HZ}"], check=True, stdout=sys.stderr)
        self.proc = subprocess.Popen([str(binary), "--node", str(node_id), "--baud", str(SIM_BAUD), "--clk", str(SIM_CLK_HZ)],
                                     stdout=subprocess.PIPE, text=True)
        line = self.proc.stdout.readline().strip()
        if not line.startswith("PTY "):
            raise RuntimeError(f"virtual board did not report a pty: {line!r}")
        super().__init__(line[4:], baud=115200)   # baud here is meaningless on a pty

    def close(self) -> None:
        super().close()
        self.proc.terminate()
