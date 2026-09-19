"""Assert rtl/orbit_params.vh is exactly what orbit/params.py generates
(`python -m orbit.params --emit-vh` rewrites it). Run directly or via
tests/test_params_check.py; tb/run_all.py runs it before any lint."""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HEADER = ROOT / "rtl" / "orbit_params.vh"


def check() -> list[str]:
    sys.path.insert(0, str(ROOT))
    from orbit import params
    want = params.emit_vh()
    if not HEADER.exists():
        return [f"{HEADER.name}: missing; run `uv run python -m orbit.params --emit-vh`"]
    got = HEADER.read_text()
    if got == want:
        return []
    diff = [f"{HEADER.name} differs from params.py output; run `uv run python -m orbit.params --emit-vh`"]
    for a, b in zip(got.splitlines(), want.splitlines()):
        if a != b:
            diff.append(f"  header: {a!r}\n  params: {b!r}")
            break
    return diff


if __name__ == "__main__":
    errs = check()
    for e in errs:
        print("MISMATCH:", e)
    print("params_check:", "FAIL" if errs else "OK")
    sys.exit(1 if errs else 0)
