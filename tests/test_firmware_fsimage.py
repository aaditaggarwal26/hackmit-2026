"""Prove the flash image's checksum means the same thing in Python and on the ESP32.

A CRC32 with the wrong polynomial, init or reflection is the classic silent mismatch: the
builder writes one, the firmware computes another, every frame fails, and the "integrity check"
that was supposed to catch a bad flash is itself the thing that is broken. Arguing about it is
not enough, so it is not argued about -- firmware/test/fsimage_host.cpp compiles the SAME header
the board compiles, and every blob of the real image set is pushed through both implementations.

The other half is behaviour: a good image verifies clean, and each way a `write-flash` can go
wrong (a flipped byte, a short write, a file that never landed) produces the right verdict for
the right path rather than a generic failure.
"""

import shutil
import subprocess
import zlib
from pathlib import Path

import pytest

import orbit.corpus as oc
from tools.build_fs_image import MANIFEST_FMT, SAT_NODE_ID, build_image, capture_sequence, stage

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "firmware/test/fsimage_host.cpp"
INC = ROOT / "firmware/satellite_esp32"
HEADER = INC / "orbit_fsimage.h"

pytestmark = pytest.mark.skipif(shutil.which("g++") is None, reason="no g++ to build the host harness")


@pytest.fixture(scope="module")
def host(tmp_path_factory):
    """Build the firmware header into a host binary. A compile error is a test failure."""
    exe = tmp_path_factory.mktemp("fw") / "fsimage_host"
    subprocess.run(
        ["g++", "-O2", "-std=c++17", "-Wall", "-Wextra", "-Werror", f"-I{INC}", str(SRC), "-o", str(exe)],
        check=True,
        capture_output=True,
        text=True,
    )
    return exe


@pytest.fixture(scope="module")
def image():
    """The real image set satellite B is flashed with: manifest plus every blob's exact bytes."""
    corp = oc.load()
    seq = capture_sequence(corp, SAT_NODE_ID["b"], 0, 40)
    manifest, blobs = build_image(corp, seq, "b")
    return seq, manifest, blobs


def crc_of(exe: Path, data: bytes) -> str:
    r = subprocess.run([str(exe), "crc"], input=data, capture_output=True)
    assert r.returncode == 0, r.stderr
    return r.stdout.decode().strip()


def verify(exe: Path, root: Path, manifest: dict, fmt: int | None = None) -> list[str]:
    rows = [f"{fmt if fmt is not None else manifest['fmt']} {len(manifest['frames'])}"]
    rows += [f"{e['ref']} {e['crc32']:08x} {e['ref_crc32']:08x}" for e in manifest["frames"]]
    r = subprocess.run([str(exe), "verify", str(root)], input="\n".join(rows) + "\n", capture_output=True, text=True)
    assert r.returncode == 0, f"{r.stdout}{r.stderr}"
    return r.stdout.splitlines()


def test_host_selftest_passes(host):
    """The header's own vectors, headed by crc32("123456789") == 0xCBF43926.

    That one constant pins the polynomial, the init value, the reflection and the final xor
    simultaneously: any of the four wrong and it is some other number.
    """
    assert crc_of(host, b"123456789") == "cbf43926"
    r = subprocess.run([str(host), "selftest"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
    assert r.stdout.splitlines()[-1].startswith("OK:")


def test_crc_agrees_with_zlib_over_every_blob_of_the_real_image(host, image):
    """Not a synthetic buffer: the actual frames and references flashed onto satellite B.

    Real imagery is what makes this worth running -- it is 8-bit gray with every byte value in
    it, including the high-bit bytes a reflection error mangles, and 16384 bytes is long enough
    that an init-value error cannot cancel out.
    """
    _, _, blobs = image
    assert len(blobs) >= 40
    for name, data in sorted(blobs.items()):
        assert crc_of(host, data) == f"{zlib.crc32(data):08x}", name


def test_manifest_carries_a_checksum_for_every_frame_without_changing_the_selection(image):
    """Integrity data is added; the capture order is not touched.

    tests/test_firmware_config.py holds the flash capture order to the simulator's. This asserts
    the other half of that promise from the manifest's side: the frames are still 0..N-1 in the
    sequence capture_sequence() produced, and the only thing that is new is the checksums.
    """
    seq, manifest, blobs = image
    assert manifest["fmt"] == MANIFEST_FMT
    assert manifest["sat"] == "b"
    assert [e["frame"] for e in manifest["frames"]] == list(range(len(seq)))
    corp = oc.load()
    assert [e["ref"] for e in manifest["frames"]] == [corp.reference_for(c) for c in seq]
    for e in manifest["frames"]:
        assert e["crc32"] == zlib.crc32(blobs[f"frames/{e['frame']:03d}.bin"])
        assert e["ref_crc32"] == zlib.crc32(blobs[f"refs/{e['ref']:03d}.bin"])


def test_manifest_format_constant_matches_the_firmware():
    """A builder and a firmware that disagree about the format number is the one failure this
    whole mechanism cannot report, because the report is what would be mis-triggered."""
    text = HEADER.read_text()
    assert f"#define ORBIT_MANIFEST_FMT {MANIFEST_FMT}\n" in text


def test_a_good_image_verifies_clean(host, image, tmp_path):
    _, manifest, blobs = image
    stage(tmp_path, manifest, blobs)
    assert verify(host, tmp_path, manifest) == ["bad=0"]


@pytest.mark.parametrize("target", ["frames/007.bin", "refs"])
def test_a_flipped_byte_is_caught_on_the_blob_that_holds_it(host, image, tmp_path, target):
    """One bit of one byte, in a frame and in a reference. A reference matters as much: the
    change term of every frame in its scene is measured against it."""
    _, manifest, blobs = image
    stage(tmp_path, manifest, blobs)
    name = target if target.endswith(".bin") else sorted(k for k in blobs if k.startswith("refs/"))[0]
    p = tmp_path / name
    data = bytearray(p.read_bytes())
    data[9000] ^= 0x01
    p.write_bytes(data)

    out = verify(host, tmp_path, manifest)
    assert out[-1] != "bad=0"
    bad = [line for line in out if line.startswith("CHECKSUM MISMATCH")]
    assert bad, out
    for line in bad:
        _, path, want, got = line.split("|")
        assert path == "/" + name
        assert want != got
        assert want == f"{zlib.crc32(blobs[name]):08x}"


def test_a_short_write_and_a_missing_file_are_both_caught(host, image, tmp_path):
    """`esptool write-flash` that browns out mid-transfer leaves exactly these two shapes."""
    _, manifest, blobs = image
    stage(tmp_path, manifest, blobs)
    short = tmp_path / "frames/012.bin"
    short.write_bytes(short.read_bytes()[:4096])  # truncated: reads back under FRAME_BYTES
    (tmp_path / "frames/000.bin").unlink()  # never landed at all

    out = verify(host, tmp_path, manifest)
    missing = [line.split("|")[1] for line in out if line.startswith("missing or short")]
    assert "/frames/012.bin" in missing
    assert "/frames/000.bin" in missing
    assert out[-1] == f"bad={len(missing)}"


def test_an_older_manifest_is_refused_once_rather_than_failing_every_frame(host, image, tmp_path):
    """A board still carrying a pre-checksum image has no checksums to check.

    Reporting that as forty corrupt frames would send the operator hunting a flash fault that
    does not exist. It is one fatal line that says what it actually is: reflash.
    """
    _, manifest, blobs = image
    stage(tmp_path, manifest, blobs)
    out = verify(host, tmp_path, manifest, fmt=MANIFEST_FMT - 1)
    assert out == [
        f"manifest predates per-frame checksums|/manifest.json|{MANIFEST_FMT:08x}|{MANIFEST_FMT - 1:08x}",
        "bad=-1",
    ]
