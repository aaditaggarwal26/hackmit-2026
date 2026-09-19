"""NodeTransport: one interface, three implementations, chosen by path scheme.
Everything above this layer (orchestrator, dashboard, bench) is written once."""
from __future__ import annotations

import queue
from abc import ABC, abstractmethod


class NodeTransport(ABC):
    node_id: int | None = None   # set by open_transport: parsed from sim://n / verilator://n, given for hardware

    @abstractmethod
    def send(self, frame: bytes) -> None: ...

    @abstractmethod
    def recv(self, timeout: float) -> bytes | None:
        """Raw bytes (any chunking); None on timeout."""

    def close(self) -> None:
        pass


class HardwareNode(NodeTransport):
    """pyserial to an Arty. Select the port by FTDI serial number, not glob
    order: the FT2232H exposes two ttys per board on macOS and UART is
    channel B (the higher-numbered one).

    send() never blocks: bytes go to a queue drained by one writer thread per
    port. A 1024-slot push is ~56 KB, ~4.9 s at 115200 baud; a blocking write
    inside the orchestrator loop would starve the other node of HEARTBEATs
    for longer than LINK_TIMEOUT_MS and flap its link_ok every segment.
    Order is preserved (single queue, single writer)."""

    def __init__(self, path: str, baud: int):
        import queue
        import threading

        import serial  # lazy: not needed for sim-only runs
        self.ser = serial.Serial(path, baud, timeout=0)
        self._q: queue.Queue[bytes | None] = queue.Queue()
        self._writer = threading.Thread(target=self._pump, name=f"uart-tx {path}", daemon=True)
        self._writer.start()
        self._q.put(b"\x00")  # link-open delimiter so the peer starts on a frame boundary

    def _pump(self) -> None:
        while (data := self._q.get()) is not None:
            try:
                self.ser.write(data)
            except Exception:      # port closed underneath us: stop quietly, close() joins
                return

    def send(self, frame: bytes) -> None:
        self._q.put(frame)

    def recv(self, timeout: float) -> bytes | None:
        if self.ser.timeout != timeout:   # the setter reconfigures the port (tcsetattr); not per poll
            self.ser.timeout = timeout
        data = self.ser.read(4096)
        return data or None

    def close(self) -> None:
        self._q.put(None)
        self._writer.join(timeout=5)
        self.ser.close()


class QueueTransport(NodeTransport):
    """In-memory byte queues; the node side runs in-process (SimulatedNode)
    or as a Verilator subprocess pump (VerilatorNode)."""

    def __init__(self):
        self.to_node: queue.Queue[bytes] = queue.Queue()
        self.from_node: queue.Queue[bytes] = queue.Queue()

    def send(self, frame: bytes) -> None:
        self.to_node.put(frame)

    def recv(self, timeout: float) -> bytes | None:
        try:
            return self.from_node.get(timeout=timeout)
        except queue.Empty:
            return None


def open_transport(path: str, baud: int, node_id: int | None = None) -> NodeTransport:
    if path.startswith("sim://"):
        from orbit.golden.node import SimulatedNode  # golden model in-process
        t = SimulatedNode(node_id=int(path[6:]))
    elif path.startswith("verilator://"):
        from orbit.protocol.verilator_node import VerilatorNode  # compiled RTL on a pty
        t = VerilatorNode(node_id=int(path[12:]), baud=baud)
    else:
        t = HardwareNode(path, baud)   # id is whatever the board reports; caller may pass its expectation
    t.node_id = node_id if node_id is not None else (int(path.split("://")[1]) if "://" in path else None)
    return t
