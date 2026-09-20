"""Hold the firmware's payload codec to orbit/protocol/codec.py, on real corpus frames.

There is no arduino-cli here, so the .ino cannot be compiled and nothing in it can be claimed
to work. firmware/satellite_esp32/orbit_codec.h exists so that the part that is *encoding*
rather than I/O can be: it is Arduino-free, the ESP32 builds the same file against miniz, and
g++ builds it here against the system zlib.

What is actually being proved is narrower and more important than "both sides compress". The
two sides do NOT have to emit the same bytes -- miniz and zlib are both conforming DEFLATE
encoders and may choose differently -- so the tests below check the only equality the protocol
rests on: whatever the firmware transmits, the ground inflates back to the exact 16384 bytes
with the exact sha256 the satellite put in tx_done, and vice versa.
"""

import hashlib
import shutil
import subprocess
from pathlib import Path

import pytest

from orbit import config, corpus
from orbit.protocol import codec

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware/test/codec_host.cpp"
INC = ROOT / "firmware/satellite_esp32"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="no g++ to build the host harness")


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Build the firmware header into a host binary. A compile error is a test failure."""
    exe = tmp_path_factory.mktemp("fw") / "codec_host"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{INC}", str(SRC), "-o", str(exe), "-lz"],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


@pytest.fixture(scope="module")
def frames():
    return [bytes(f) for f in corpus.load().frames]


def run(exe: Path, mode: str, stdin: str = "") -> list[str]:
    r = subprocess.run([str(exe), mode], input=stdin, capture_output=True, text=True)
    assert r.returncode == 0, f"{mode} failed ({r.returncode}): {r.stdout}{r.stderr}"
    return r.stdout.splitlines()


def pipe(exe: Path, mode: str, data: bytes) -> bytes:
    r = subprocess.run([str(exe), mode], input=data, capture_output=True)
    assert r.returncode == 0, f"{mode} failed ({r.returncode}): {r.stderr.decode(errors='replace')}"
    return r.stdout


def test_host_selftest_passes(host):
    """The header's own table of cases: chunk arithmetic, round trip, idempotence, raw fallback."""
    assert run(host, "selftest")[-1].startswith("OK:")


def test_chunk_arithmetic_matches_the_ground_exactly(host):
    """The ground rejects any idx outside 0..chunks-1, so a disagreement here loses whole frames."""
    cases = [(n, c) for c in (1, 7, 512, 900, 4096) for n in (0, 1, 2, 899, 900, 901, 11245, config.FRAME_BYTES)]
    stdin = "".join(f"{n} {c}\n" for n, c in cases)
    got = [int(x) for x in run(host, "chunks", stdin)]
    assert got == [codec.chunk_count(n, c) for n, c in cases]


def test_the_ground_recovers_every_corpus_frame_the_firmware_would_send(host, frames):
    """The firmware's direction: its compressed payload, inflated by the ground's codec.

    This is the digest contract end to end — tx_done's sha256 is over the raw frame, and the
    ground only ever sees the compressed blob.
    """
    for raw in frames:
        payload = pipe(host, "deflate", raw)
        frame = codec.decompress(codec.ENC_ZLIB, payload, config.FRAME_BYTES)
        assert frame == raw
        assert hashlib.sha256(frame).hexdigest() == hashlib.sha256(raw).hexdigest()


def test_the_firmware_recovers_every_frame_the_ground_would_send(host, frames):
    """The other direction. Nothing on the downlink needs it, but a one-way codec is not a codec."""
    for raw in frames:
        assert pipe(host, "inflate", codec.deflate(raw)) == raw


def test_both_sides_choose_the_same_encoding(host, frames):
    """compress() and orbit_tx_prepare() must agree on when compressing is worth it.

    They are allowed to produce different bytes; they are not allowed to disagree about whether
    the payload is a zlib stream, because that label is what the ground decodes by.
    """
    for raw in frames[:24]:
        got = subprocess.run([str(host), "enc"], input=raw, capture_output=True).stdout.decode().strip()
        assert got == codec.compress(raw)[0]


def test_an_incompressible_frame_falls_back_to_raw_on_both_sides(host):
    noise = hashlib.sha256(b"seed").digest()
    while len(noise) < config.FRAME_BYTES:
        noise += hashlib.sha256(noise[-32:]).digest()
    noise = noise[: config.FRAME_BYTES]
    got = subprocess.run([str(host), "enc"], input=noise, capture_output=True).stdout.decode().strip()
    assert got == codec.ENC_RAW == codec.compress(noise)[0]
    assert pipe(host, "deflate", noise) == noise  # the payload IS the frame: nothing to decode


def test_the_firmware_is_byte_for_byte_repeatable(host, frames):
    """TX_PASSES re-sends the whole sequence and the ground keys chunks by idx: pass 3 must equal pass 1."""
    for raw in frames[:8]:
        assert pipe(host, "deflate", raw) == pipe(host, "deflate", raw)
