"""Energy samplers, one interface: start(); stop() -> dict(joules, seconds,
mean_watts, label, source). `label` is the provenance a judge will probe:
  device-level  Jetson VDD_CPU_GPU_CV / VDD_SOC rails; Vivado on-chip estimate
  module-level  Jetson VDD_IN (module input; excludes the dev-kit carrier)
  whole-board   Arty via INA219 on its supply input (includes FTDI, regulators)
  estimate      Vivado report_power (not a measurement)
Anything not measured is TBD — joules=None — never a placeholder number."""
from __future__ import annotations

import glob
import os
import re
import subprocess
import threading
import time

from orbit.protocol import messages as M
from .ina219 import convert

TBD = "TBD — pending measurement"


def tbd(source: str, **extra) -> dict:
    return dict(joules=None, seconds=None, mean_watts=None, label=TBD, source=source, **extra)


def result(joules: float, seconds: float, label: str, source: str, **extra) -> dict:
    return dict(joules=joules, seconds=seconds, mean_watts=joules / seconds if seconds else None,
                label=label, source=source, **extra)


class Sampler:
    def start(self) -> None: ...
    def stop(self) -> dict: ...


class NoSampler(Sampler):
    def stop(self) -> dict:
        return tbd("none")


# --- (a) Jetson Orin Nano: INA3221 via hwmon sysfs ---------------------------------
JETSON_LABELS = {
    "VDD_IN": "module-level (Jetson VDD_IN)",
    "VDD_CPU_GPU_CV": "device-level (Jetson VDD_CPU_GPU_CV rail)",
    "VDD_SOC": "device-level (Jetson VDD_SOC rail)",
}


class JetsonINA3221Sampler(Sampler):
    """Samples every rail the driver exposes (label from in{i}_label) at ~hz in a
    thread, integrates V*I over measured wall-clock deltas. The primary rail is
    what stop() reports; every rail is in result['rails']."""

    def __init__(self, base: str = "/sys/bus/i2c/drivers/ina3221/1-0040", rail: str = "VDD_CPU_GPU_CV",
                 hz: float = 20.0):
        self.base, self.rail, self.period = base, rail, 1.0 / hz
        self.channels = self._discover()      # label -> (volt_path, curr_path)
        self.joules = {k: 0.0 for k in self.channels}
        self.n, self.span = 0, 0.0        # samples, integrated seconds
        self.error = None
        self._stop = threading.Event()
        self._thread = None

    def _discover(self) -> dict:
        out = {}
        for d in glob.glob(os.path.join(self.base, "hwmon", "hwmon*")):
            for lab in glob.glob(os.path.join(d, "in*_label")):
                i = re.search(r"in(\d+)_label$", lab).group(1)
                with open(lab) as f:
                    name = f.read().strip()
                v, c = os.path.join(d, f"in{i}_input"), os.path.join(d, f"curr{i}_input")
                if os.path.exists(v) and os.path.exists(c):
                    out[name] = (v, c)
        return out

    def read_watts(self) -> dict:
        w = {}
        for name, (v, c) in self.channels.items():
            with open(v) as fv, open(c) as fc:
                w[name] = int(fv.read()) / 1000.0 * int(fc.read()) / 1000.0   # mV * mA
        return w

    def _loop(self):
        try:
            prev_t, prev_w = time.monotonic(), self.read_watts()
            while not self._stop.wait(self.period):
                t, w = time.monotonic(), self.read_watts()
                for k in w:
                    self.joules[k] += 0.5 * (w[k] + prev_w[k]) * (t - prev_t)   # trapezoid, measured dt
                self.span += t - prev_t
                prev_t, prev_w, self.n = t, w, self.n + 1
        except OSError as e:               # a dead sysfs read must not become a silently short integral
            self.error = f"{e} after {self.n} samples"

    def start(self):
        if not self.channels:
            return
        self.t0 = time.monotonic()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()

    def stop(self) -> dict:
        if not self.channels:
            return tbd("ina3221", note=f"no hwmon channels under {self.base}")
        self._stop.set()
        self._thread.join()
        wall, secs = time.monotonic() - self.t0, self.span     # mean W over the integrated span, not the wall clock
        if self.error or not secs:
            return tbd("ina3221", note=self.error or "fewer than two samples")
        rails = {JETSON_LABELS.get(k, f"unlabelled ({k})"): dict(joules=j, mean_watts=j / secs)
                 for k, j in self.joules.items()}
        if self.rail not in self.joules:
            return tbd("ina3221", note=f"rail {self.rail} not present; have {list(self.joules)}", rails=rails)
        return result(self.joules[self.rail], secs, JETSON_LABELS.get(self.rail, self.rail), "ina3221",
                      samples=self.n, wall_seconds=wall, rails=rails)


# --- (b) macOS powermetrics: dev-machine fallback ----------------------------------
MAC_LABEL = "device-level (package), dev machine only"
_PM_ELAPSED = re.compile(r"\((\d+(?:\.\d+)?)ms elapsed\)")
_PM_CPU = re.compile(r"^CPU Power:\s*(\d+(?:\.\d+)?)\s*mW", re.M)


def parse_powermetrics(text: str, interval_s: float) -> list[tuple[float, float]]:
    """-> [(watts, dt_s)] per sample block. dt from the block's '(NNNms elapsed)'
    header when present, else the requested interval."""
    blocks = re.split(r"\*\*\* Sampled system activity", text)[1:] if "*** Sampled" in text else [text]
    out = []
    for block in blocks:
        cpu = _PM_CPU.search(block)
        if not cpu:
            continue
        el = _PM_ELAPSED.search(block)
        out.append((float(cpu.group(1)) / 1000.0, float(el.group(1)) / 1000.0 if el else interval_s))
    return out


class MacPowermetricsSampler(Sampler):
    def __init__(self, interval_ms: int = 200):
        self.interval_ms = interval_ms
        self.proc = None

    def start(self):
        self.t0 = time.monotonic()
        self.proc = subprocess.Popen(["sudo", "powermetrics", "--samplers", "cpu_power", "-i", str(self.interval_ms)],
                                     stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)

    def stop(self) -> dict:
        secs = time.monotonic() - self.t0
        self.proc.terminate()
        text, _ = self.proc.communicate(timeout=10)
        samples = parse_powermetrics(text, self.interval_ms / 1000.0)
        if not samples:
            return tbd("powermetrics", note="no 'CPU Power' samples (needs sudo)")
        joules = sum(w * dt for w, dt in samples)
        return result(joules, sum(dt for _, dt in samples), MAC_LABEL, "powermetrics",
                      samples=len(samples), wall_seconds=secs)


# --- (c) Arty: INA219 POWER frames over the protocol ---------------------------------
ARTY_LABEL = "whole-board (Arty supply input)"


class INA219Sampler(Sampler):
    """feed() every POWER message; valid=0 frames are counted, not integrated.
    Integrates watts (trapezoid) between the node's own uptime_ms stamps."""

    def __init__(self):
        self.joules = 0.0
        self.valid = self.invalid = 0
        self.prev = None          # (uptime_ms, watts)
        self.t_first = None
        self.last = None

    def feed(self, msg: M.Power) -> None:
        if not msg.flags & M.Power.VALID:
            self.invalid += 1
            return
        self.valid += 1
        self.last = convert(msg.bus_raw, msg.shunt_raw)
        w = self.last["watts"]
        if self.prev is None:
            self.t_first = msg.uptime_ms
        else:
            dt = ((msg.uptime_ms - self.prev[0]) & 0xFFFFFFFF) / 1000.0
            self.joules += 0.5 * (w + self.prev[1]) * dt
        self.prev = (msg.uptime_ms, w)

    def consume(self, msgs) -> None:
        for m in msgs:
            if isinstance(m, M.Power):
                self.feed(m)

    def stop(self) -> dict:
        if self.valid < 2:
            return tbd("ina219", valid_frames=self.valid, invalid_frames=self.invalid,
                       note="no valid POWER frames (node reports no INA219)" if not self.valid
                       else "one valid frame: no interval to integrate")
        secs = ((self.prev[0] - self.t_first) & 0xFFFFFFFF) / 1000.0
        return result(self.joules, secs, ARTY_LABEL, "ina219", valid_frames=self.valid,
                      invalid_frames=self.invalid, last=self.last)


# --- (d) Vivado report_power: an estimate, not a measurement -------------------------
VIVADO_LABEL = "estimate (Vivado report_power)"
_VIVADO_TOTAL = re.compile(r"Total On-Chip Power \(W\)\s*\|\s*(\d+(?:\.\d+)?)")


def parse_vivado_power(text: str) -> float | None:
    m = _VIVADO_TOTAL.search(text)
    return float(m.group(1)) if m else None


class VivadoPowerEstimate(Sampler):
    """Total on-chip watts from the report × wall seconds of the workload.
    Not a measurement: the label says so."""

    def __init__(self, path: str = "vivado/reports/power.txt"):
        self.path = path
        self.watts = None
        if os.path.exists(path):
            with open(path) as f:
                self.watts = parse_vivado_power(f.read())

    def start(self):
        self.t0 = time.monotonic()

    def stop(self) -> dict:
        if self.watts is None:
            return tbd("vivado", note=f"{self.path} missing or has no 'Total On-Chip Power (W)' line")
        secs = time.monotonic() - self.t0
        return result(self.watts * secs, secs, VIVADO_LABEL, "vivado", report=self.path)
