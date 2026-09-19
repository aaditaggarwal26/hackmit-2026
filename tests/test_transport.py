"""HardwareNode over a pty pair: send() must not block, bytes must arrive in order."""
import os
import time

import pytest

serial = pytest.importorskip("serial")
from orbit.protocol.transport import HardwareNode, open_transport  # noqa: E402


def test_hardware_node_send_is_nonblocking_and_ordered():
    master, slave = os.openpty()
    node = HardwareNode(os.ttyname(slave), 115200)
    payload = bytes(range(1, 256)) * 400          # ~100 KB, far more than a pty buffer
    t0 = time.monotonic()
    node.send(payload)
    assert time.monotonic() - t0 < 0.05           # queued, not written inline
    got = bytearray()
    deadline = time.monotonic() + 10
    while len(got) < 1 + len(payload) and time.monotonic() < deadline:
        try:
            got += os.read(master, 65536)
        except BlockingIOError:
            time.sleep(0.01)
    assert bytes(got) == b"\x00" + payload        # link-open delimiter first, then everything in order
    # recv path: write into the pty, read out through the transport
    os.write(master, b"\x01\x02\x03")
    time.sleep(0.05)
    assert node.recv(0.0) == b"\x01\x02\x03"
    assert node.recv(0.0) is None
    node.close()
    os.close(master)


def test_open_transport_sets_node_id_for_hardware_path():
    master, slave = os.openpty()
    t = open_transport(os.ttyname(slave), 115200, node_id=1)
    assert t.node_id == 1
    t.close(); os.close(master)
