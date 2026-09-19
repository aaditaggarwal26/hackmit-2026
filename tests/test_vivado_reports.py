"""tools.vivado_reports against hand-written fixtures in Vivado 2024.1 text
format (tests/fixtures/vivado/, each headed by a '# FIXTURE' line the parser
must skip). Nothing here is a measurement."""
import json
import shutil
from datetime import datetime
from pathlib import Path

import pytest

from tools import vivado_reports as vr

FIX = Path(__file__).parent / "fixtures" / "vivado"
TBD = vr.TBD


def read(name: str) -> str:
    return (FIX / name).read_text()


# --- fixtures are labelled, and the label does not break parsing ----------------------
@pytest.mark.parametrize("name", ["utilization.txt", "timing.txt", "power.txt"])
def test_fixture_is_labelled_not_measured(name):
    assert read(name).splitlines()[0].startswith("# FIXTURE")


@pytest.mark.parametrize("name,fn", [("utilization.txt", vr.parse_utilization), ("timing.txt", vr.parse_timing),
                                     ("power.txt", vr.parse_power)])
def test_parsers_accept_crlf_from_a_windows_build(name, fn):
    text = read(name)
    assert fn(text.replace("\n", "\r\n")) == fn(text)


# --- report_utilization -----------------------------------------------------------------
def test_parse_utilization_fixture():
    u = vr.parse_utilization(read("utilization.txt"))
    assert u["slice_luts"] == {"used": 12345, "available": 63400, "pct": 19.47}
    assert u["slice_registers"] == {"used": 23456, "available": 126800, "pct": 18.50}
    assert u["bram_tiles"] == {"used": 48, "available": 135, "pct": 35.56}
    assert u["dsps"] == {"used": 50, "available": 240, "pct": 20.83}


def test_utilization_slice_registers_takes_section_1_not_distribution():
    text = ("2. Slice Logic Distribution\n"
            "| Slice Registers | 999 | 0 | 0 | 126800 | 0.79 |\n")
    sec1 = ("1. Slice Logic\n"
            "| Slice Registers | 23456 | 0 | 0 | 126800 | 18.50 |\n")
    assert vr.parse_utilization(sec1 + text)["slice_registers"]["used"] == 23456


def test_utilization_dsp_child_row_is_not_the_dsps_row():
    child_only = "| Site Type | Used |\n|   DSP48E1 only |   50 |       |            |           |       |\n"
    assert vr.parse_utilization(child_only)["dsps"] == TBD


def test_utilization_older_four_column_layout():
    text = ("| Slice LUTs | 100 | 0 | 63400 | 0.16 |\n"
            "| DSPs       |   3 | 0 |   240 | 1.25 |\n")
    u = vr.parse_utilization(text)
    assert u["slice_luts"] == {"used": 100, "available": 63400, "pct": 0.16}
    assert u["dsps"] == {"used": 3, "available": 240, "pct": 1.25}
    assert u["bram_tiles"] == TBD and u["slice_registers"] == TBD


def test_utilization_empty_is_all_tbd():
    assert vr.parse_utilization("") == {k: TBD for k in vr.UTIL_ROWS}


# --- report_timing_summary -----------------------------------------------------------------
def test_parse_timing_fixture():
    t = vr.parse_timing(read("timing.txt"))
    assert t == {"wns_ns": 1.234, "tns_ns": 0.0, "whs_ns": 0.123, "met": True}


NOT_MET = """| Design Timing Summary
| ---------------------
------------------------------------------------------------------------------------------------

    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints  WPWS(ns)  TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints
    -------  -------  ---------------------  -------------------  -------  -------  ---------------------  -------------------  --------  --------  ----------------------  --------------------
     -0.517  -12.345                     31                45678    0.050    0.000                      0                45678     4.020     0.000                       0                 23456


Timing constraints are not met.
"""


def test_timing_negative_wns_and_not_met_sentence():
    t = vr.parse_timing(NOT_MET)
    assert t == {"wns_ns": -0.517, "tns_ns": -12.345, "whs_ns": 0.05, "met": False}


def test_timing_met_falls_back_to_slack_sign_when_sentence_absent():
    body = NOT_MET.replace("Timing constraints are not met.\n", "")
    assert vr.parse_timing(body)["met"] is False
    assert vr.parse_timing(body.replace("-0.517", " 0.517"))["met"] is True


def test_timing_na_columns_are_tbd():
    block = ("| Design Timing Summary\n"
             "    WNS(ns)  TNS(ns)  TNS Failing Endpoints  TNS Total Endpoints  WHS(ns)  THS(ns)  THS Failing Endpoints  THS Total Endpoints  WPWS(ns)  TPWS(ns)  TPWS Failing Endpoints  TPWS Total Endpoints  \n"
             "    -------  -------  ---------------------  -------------------  -------  -------  ---------------------  -------------------  --------  --------  ----------------------  --------------------  \n"
             "         NA       NA                      0                    0       NA       NA                      0                    0        NA        NA                       0                     0  \n")
    assert vr.parse_timing(block) == {"wns_ns": TBD, "tns_ns": TBD, "whs_ns": TBD, "met": TBD}


def test_timing_missing_block_is_tbd():
    assert vr.parse_timing("no summary here") == {"wns_ns": TBD, "tns_ns": TBD, "whs_ns": TBD, "met": TBD}


# --- report_power ----------------------------------------------------------------------------
def test_parse_power_fixture_reuses_energy_parser():
    from orbit.bench.energy import parse_vivado_power
    text = read("power.txt")
    p = vr.parse_power(text)
    assert p == {"total": 0.512, "dynamic": 0.415, "static": 0.097}
    assert p["total"] == parse_vivado_power(text)


def test_power_total_only():
    p = vr.parse_power("| Total On-Chip Power (W)  | 0.300        |\n")
    assert p == {"total": 0.3, "dynamic": TBD, "static": TBD}


def test_power_empty_is_tbd():
    assert vr.parse_power("") == {"total": TBD, "dynamic": TBD, "static": TBD}


# --- summary --------------------------------------------------------------------------------------
def test_build_summary_all_reports_is_measured():
    s = vr.build_summary(str(FIX))
    assert s["source"] == "vivado" and s["measured"] is True
    assert s["utilization"]["dsps"]["used"] == 50
    assert s["timing"] == {"wns_ns": 1.234, "tns_ns": 0.0, "whs_ns": 0.123, "met": True}
    assert s["power_w"]["total"] == 0.512 and s["power_w"]["label"] == "estimate (Vivado report_power)"
    assert all(v is not None for v in s["reports"].values())
    datetime.fromisoformat(s["generated_at"])
    assert TBD not in json.dumps(s, ensure_ascii=False)


def test_build_summary_missing_dir_is_all_tbd(tmp_path):
    s = vr.build_summary(str(tmp_path / "nope"))
    assert s["measured"] is False
    assert s["utilization"] == {k: TBD for k in vr.UTIL_ROWS}
    assert s["timing"] == {"wns_ns": TBD, "tns_ns": TBD, "whs_ns": TBD, "met": TBD}
    assert s["power_w"] == {"total": TBD, "dynamic": TBD, "static": TBD, "label": "estimate (Vivado report_power)"}
    assert s["reports"] == {"utilization": None, "timing": None, "power": None}


def test_build_summary_partial_reports_not_measured(tmp_path):
    shutil.copy(FIX / "power.txt", tmp_path / "power.txt")
    s = vr.build_summary(str(tmp_path))
    assert s["measured"] is False and s["power_w"]["total"] == 0.512
    assert s["utilization"]["slice_luts"] == TBD and s["timing"]["met"] == TBD
    assert s["reports"]["power"] and s["reports"]["timing"] is None


def test_build_summary_unparseable_row_is_not_measured(tmp_path):
    for n in ("utilization.txt", "timing.txt", "power.txt"):
        shutil.copy(FIX / n, tmp_path / n)
    (tmp_path / "utilization.txt").write_text(read("utilization.txt").replace("| DSPs ", "| XXXX "))
    s = vr.build_summary(str(tmp_path))
    assert s["measured"] is False and s["utilization"]["dsps"] == TBD and s["utilization"]["slice_luts"] != TBD


def test_write_and_load_summary_roundtrip(tmp_path):
    d = tmp_path / "reports"                      # does not exist yet: write_summary must create it
    path, s = vr.write_summary(str(d))
    assert path == str(d / "summary.json")
    loaded = vr.load_summary(path)
    assert loaded == s and loaded["measured"] is False
    assert loaded["utilization"]["dsps"] == TBD    # the em dash survives the JSON round trip


def test_load_summary_missing_file_is_empty_summary(tmp_path):
    s = vr.load_summary(str(tmp_path / "summary.json"))
    assert s["measured"] is False and s["generated_at"] is None
    assert s == vr.empty_summary()


# --- CLI ---------------------------------------------------------------------------------------------
def test_cli_with_reports(tmp_path, capsys):
    for n in ("utilization.txt", "timing.txt", "power.txt"):
        shutil.copy(FIX / n, tmp_path / n)
    assert vr.main(["--reports-dir", str(tmp_path)]) == 0
    out = capsys.readouterr().out
    assert "measured=True" in out and "missing" not in out
    assert json.load(open(tmp_path / "summary.json"))["measured"] is True


def test_cli_without_reports_exits_zero_and_writes_tbd(tmp_path, capsys):
    d = tmp_path / "vivado" / "reports"
    assert vr.main(["--reports-dir", str(d)]) == 0
    out = capsys.readouterr().out
    assert "measured=False" in out and "utilization.txt, timing.txt, power.txt" in out
    s = json.load(open(d / "summary.json"))
    assert s["measured"] is False and s["timing"]["wns_ns"] == TBD
