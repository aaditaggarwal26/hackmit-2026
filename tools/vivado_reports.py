"""Parse the three text reports the Vivado build writes to vivado/reports/
(report_utilization, report_timing_summary, report_power) into one
summary.json the dashboard reads to flip its FPGA figures from "estimated"
to "measured".

    uv run python -m tools.vivado_reports [--reports-dir vivado/reports]

Every extractor is a regex search over the whole file, so a leading comment
line (the fixtures carry one) is skipped for free. Anything that cannot be
parsed is the literal TBD string, never a number; `measured` is true only
when all three reports are present and every field parsed. Missing reports
are not an error: summary.json is still written and the exit code is 0.
Total on-chip power comes from orbit.bench.energy.parse_vivado_power."""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

from orbit.bench.energy import parse_vivado_power

TBD = "TBD — pending Vivado report"
DEFAULT_REPORTS_DIR = "vivado/reports"
REPORT_FILES = {"utilization": "utilization.txt", "timing": "timing.txt", "power": "power.txt"}

# --- report_utilization -------------------------------------------------------------
# Row names as Vivado prints them; the key is what summary.json uses.
# "Slice Registers" also appears under "Slice Logic Distribution" and the DSP
# section has a child row "DSP48E1 only": anchoring to the row start and taking
# the first match keeps section 1 / the parent row.
UTIL_ROWS = {
    "slice_luts": r"Slice LUTs\*?",
    "slice_registers": r"Slice Registers",
    "bram_tiles": r"Block RAM Tile",
    "dsps": r"DSPs",
}
_NUM = r"-?\d+(?:\.\d+)?"


def _util_row(text: str, row: str) -> dict | None:
    m = re.search(rf"^\|\s*{row}\s*\|(.*)$", text, re.M)
    if not m:
        return None
    cells = [c.strip() for c in m.group(1).strip().strip("|").split("|")]
    nums = [c for c in cells if re.fullmatch(_NUM, c)]
    # 2023/2024: Used | Fixed | Prohibited | Available | Util%; older: Used | Fixed | Available | Util%.
    if len(nums) < 3:
        return None
    return {"used": int(float(nums[0])), "available": int(float(nums[-2])), "pct": float(nums[-1])}


def parse_utilization(text: str) -> dict:
    """-> {slice_luts|slice_registers|bram_tiles|dsps: {used, available, pct} | TBD}."""
    return {k: _util_row(text, row) or TBD for k, row in UTIL_ROWS.items()}


# --- report_timing_summary ------------------------------------------------------------
_MET = re.compile(r"All user specified timing constraints are met\.")
_NOT_MET = re.compile(r"Timing constraints are not met\.")


def _num_or_na(tok: str) -> float | str:
    return TBD if tok.upper() == "NA" else float(tok)


def parse_timing(text: str) -> dict:
    """-> {wns_ns, tns_ns, whs_ns, met} from the 'Design Timing Summary' block.
    Columns come from the header line (WNS(ns) TNS(ns) ... WHS(ns) ...), not
    fixed positions. `met` is the report's own sentence; if neither sentence
    is present it falls back to wns >= 0 and whs >= 0."""
    out = {"wns_ns": TBD, "tns_ns": TBD, "whs_ns": TBD, "met": TBD}
    at = text.find("Design Timing Summary")
    if at >= 0:
        # header names contain spaces ("TNS Failing Endpoints"): split on 2+ spaces;
        # the value row is the next non-blank line after the dashes underline.
        lines = [l for l in text[at:].splitlines() if l.strip()]
        for i, line in enumerate(lines):
            if "WNS(ns)" in line:
                hdr = re.split(r"\s{2,}", line.strip())
                vals = next((l.split() for l in lines[i + 1:] if not re.fullmatch(r"[-\s]+", l)), [])
                if len(vals) == len(hdr):
                    col = dict(zip(hdr, vals))
                    out["wns_ns"] = _num_or_na(col["WNS(ns)"])
                    out["tns_ns"] = _num_or_na(col.get("TNS(ns)", "NA"))
                    out["whs_ns"] = _num_or_na(col.get("WHS(ns)", "NA"))
                break
    if _MET.search(text):
        out["met"] = True
    elif _NOT_MET.search(text):
        out["met"] = False
    elif out["wns_ns"] != TBD:
        out["met"] = out["wns_ns"] >= 0 and (out["whs_ns"] == TBD or out["whs_ns"] >= 0)
    return out


# --- report_power -----------------------------------------------------------------------
_DYNAMIC = re.compile(r"\|\s*Dynamic \(W\)\s*\|\s*(\d+(?:\.\d+)?)")
_STATIC = re.compile(r"\|\s*Device Static \(W\)\s*\|\s*(\d+(?:\.\d+)?)")


def parse_power(text: str) -> dict:
    """-> {total, dynamic, static} watts. Total via energy.parse_vivado_power;
    dynamic/static are optional rows and TBD when absent. This is Vivado's
    estimate, not a measurement; the summary's 'label' says so."""
    total = parse_vivado_power(text)
    dyn, sta = _DYNAMIC.search(text), _STATIC.search(text)
    return {"total": total if total is not None else TBD,
            "dynamic": float(dyn.group(1)) if dyn else TBD,
            "static": float(sta.group(1)) if sta else TBD}


# --- summary ------------------------------------------------------------------------------
def empty_summary() -> dict:
    return {"source": "vivado", "measured": False,
            "utilization": {k: TBD for k in UTIL_ROWS},
            "timing": {"wns_ns": TBD, "tns_ns": TBD, "whs_ns": TBD, "met": TBD},
            "power_w": {"total": TBD, "dynamic": TBD, "static": TBD, "label": "estimate (Vivado report_power)"},
            "reports": {k: None for k in REPORT_FILES},
            "generated_at": None}


def _all_parsed(section: dict) -> bool:
    return all(v != TBD for v in section.values())


def build_summary(reports_dir: str = DEFAULT_REPORTS_DIR) -> dict:
    """Read whichever reports exist under reports_dir. `measured` is true only
    when all three are present and every required field parsed."""
    s = empty_summary()
    found = {}
    for key, name in REPORT_FILES.items():
        path = os.path.join(reports_dir, name)
        if os.path.exists(path):
            with open(path, encoding="utf-8", errors="replace") as f:
                found[key] = f.read()
            s["reports"][key] = path
    if "utilization" in found:
        s["utilization"] = parse_utilization(found["utilization"])
    if "timing" in found:
        s["timing"] = parse_timing(found["timing"])
    if "power" in found:
        s["power_w"].update(parse_power(found["power"]))
    required = [s["utilization"], s["timing"], {"total": s["power_w"]["total"]}]   # dynamic/static optional
    s["measured"] = len(found) == len(REPORT_FILES) and all(_all_parsed(x) for x in required)
    s["generated_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return s


def write_summary(reports_dir: str = DEFAULT_REPORTS_DIR) -> tuple[str, dict]:
    s = build_summary(reports_dir)
    os.makedirs(reports_dir, exist_ok=True)
    path = os.path.join(reports_dir, "summary.json")
    with open(path, "w", encoding="utf-8") as f:
        json.dump(s, f, indent=2, ensure_ascii=False)
        f.write("\n")
    return path, s


def load_summary(path: str = os.path.join(DEFAULT_REPORTS_DIR, "summary.json")) -> dict:
    """For the dashboard: the written summary, or the all-TBD one when the
    file does not exist yet (fresh clone, build not run)."""
    if not os.path.exists(path):
        return empty_summary()
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--reports-dir", default=DEFAULT_REPORTS_DIR)
    a = ap.parse_args(argv)
    path, s = write_summary(a.reports_dir)
    missing = [REPORT_FILES[k] for k, v in s["reports"].items() if v is None]
    print(f"wrote {path}  measured={s['measured']}" + (f"  missing: {', '.join(missing)}" if missing else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
