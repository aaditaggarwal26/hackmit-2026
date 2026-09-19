"""Telemetry samplers. Each one owns a background thread, polls at `hz`, and reduces its
samples to a dict of *labelled* figures on `stop()`.

Why every figure carries scope/method/measured: on this box the only real power meter
is the GPU die's NVML counter. There is no CPU-rail or board meter at all, so a CPU
watt figure cannot exist here and must be written as "unavailable" with the reason,
never estimated from a datasheet. A sampler that cannot read its source degrades to
that same "unavailable" shape; none of them may raise out of start()/stop(), because
a broken thermometer must not abort a benchmark that is otherwise measuring."""

from __future__ import annotations

import logging
import statistics
import threading
import time
from pathlib import Path
from typing import Any, Protocol

from orbit.bench.report import measured, unavailable
from orbit.log import log

try:
    import pynvml
except ImportError:  # pragma: no cover - the venv has it; degrade to "unavailable" if not
    pynvml = None

logger = logging.getLogger("orbit.bench.sampler")

SCOPE_GPU_DIE = "gpu_die"
SCOPE_THERMAL = "board_thermal_zones"
SCOPE_CPU_UTIL = "cpu_time"
MW_PER_W = 1000.0
MJ_PER_J = 1000.0
MILLI_C_PER_C = 1000.0
THERMAL_ROOT = Path("/sys/class/thermal")
PROC_STAT = Path("/proc/stat")
THROTTLE_COOLING_TYPE = "Processor"  # PCIe link-speed cooling devices are not CPU throttling


class Sampler(Protocol):
    scope: str

    def start(self) -> None: ...

    def sample(self) -> None: ...

    def stop(self) -> dict[str, Any]: ...


class _Polled:
    """Thread plumbing shared by the real samplers: `sample()` on start, every 1/hz, and on stop."""

    scope: str = ""

    def __init__(self, hz: float) -> None:
        self.period_s = 1.0 / hz
        self.samples: list[tuple[Any, ...]] = []
        self.error: str | None = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.t_start = 0.0
        self.t_stop = 0.0

    def sample(self) -> None:
        raise NotImplementedError

    def _open(self) -> None:
        """Acquire handles; raising here makes the whole sampler "unavailable"."""

    def _reduce(self) -> dict[str, Any]:
        raise NotImplementedError

    def start(self) -> None:
        try:
            self._open()
            self.t_start = time.monotonic()
            self.sample()
        except Exception as e:  # any failure → unavailable, never an abort
            self.error = f"{type(e).__name__}: {e}"
            log(logger, logging.WARNING, "sampler unavailable", scope=self.scope, reason=self.error)
            return
        self._thread = threading.Thread(target=self._loop, name=f"sampler-{self.scope}", daemon=True)
        self._thread.start()

    def _loop(self) -> None:
        while not self._stop.wait(self.period_s):
            try:
                self.sample()
            except Exception as e:
                self.error = f"{type(e).__name__}: {e}"
                return

    def stop(self) -> dict[str, Any]:
        self._stop.set()
        if self._thread is not None:
            self._thread.join()
        try:
            if self.error is None:
                self.sample()
                self.t_stop = time.monotonic()
                return self._reduce() | {"n_samples": len(self.samples), "scope": self.scope}
        except Exception as e:
            self.error = f"{type(e).__name__}: {e}"
        return Unavailable(self.scope, self.error or "no samples").stop() | {"n_samples": len(self.samples)}

    def raw(self) -> list[list[Any]]:
        return [list(s) for s in self.samples]


class Unavailable:
    """The honest placeholder: a source that does not exist on this machine (or failed)."""

    def __init__(self, scope: str, reason: str) -> None:
        self.scope = scope
        self.reason = reason

    def start(self) -> None:
        return None

    def sample(self) -> None:
        return None

    def stop(self) -> dict[str, Any]:
        return unavailable(self.reason, "none", self.scope) | {"n_samples": 0}

    def raw(self) -> list[list[Any]]:
        return []


def _nvml_or_none(fn: Any, *args: Any) -> Any:
    """Per-field NOT_SUPPORTED (power limit, module power, memory power on GB10) becomes None."""
    try:
        return fn(*args)
    except pynvml.NVMLError:
        return None


class NvmlSampler(_Polled):
    """GPU die only. Energy from the monotonic mJ counter delta (the number that matters);
    instantaneous watts kept for mean/min/max and as a cross-check of the counter."""

    scope = SCOPE_GPU_DIE
    METHOD_POWER = "NVML nvmlDeviceGetPowerUsage (mW) polled"
    METHOD_ENERGY = "NVML nvmlDeviceGetTotalEnergyConsumption (mJ) counter delta, stop - start"
    METHOD_TEMP = "NVML nvmlDeviceGetTemperature(GPU)"
    METHOD_UTIL = (
        "NVML nvmlDeviceGetUtilizationRates.gpu; UNRELIABLE on GB10 iGPU (read 0% under a load that doubled power)"
    )

    def __init__(self, hz: float, index: int = 0) -> None:
        super().__init__(hz)
        self.index = index
        self._h: Any = None

    def _open(self) -> None:
        if pynvml is None:
            raise RuntimeError("pynvml (nvidia-ml-py) not importable")
        pynvml.nvmlInit()
        self._h = pynvml.nvmlDeviceGetHandleByIndex(self.index)

    def sample(self) -> None:
        h = self._h
        util = _nvml_or_none(pynvml.nvmlDeviceGetUtilizationRates, h)
        self.samples.append(
            (
                time.monotonic(),
                _nvml_or_none(pynvml.nvmlDeviceGetPowerUsage, h),
                _nvml_or_none(pynvml.nvmlDeviceGetTotalEnergyConsumption, h),
                _nvml_or_none(pynvml.nvmlDeviceGetTemperature, h, pynvml.NVML_TEMPERATURE_GPU),
                None if util is None else util.gpu,
            )
        )

    def _reduce(self) -> dict[str, Any]:
        watts = [s[1] / MW_PER_W for s in self.samples if s[1] is not None]
        energy = [s[2] for s in self.samples if s[2] is not None]
        temps = [s[3] for s in self.samples if s[3] is not None]
        utils = [s[4] for s in self.samples if s[4] is not None]
        hz = f"{1.0 / self.period_s:g} Hz"
        out: dict[str, Any] = {"duration_s": measured(self.t_stop - self.t_start, "s", "time.monotonic", self.scope)}
        if energy:
            out["energy_j"] = measured((energy[-1] - energy[0]) / MJ_PER_J, "J", self.METHOD_ENERGY, self.scope)
        else:
            out["energy_j"] = unavailable("energy counter NOT_SUPPORTED", self.METHOD_ENERGY, self.scope)
        if watts:
            for k, v in (("mean_w", statistics.fmean(watts)), ("min_w", min(watts)), ("max_w", max(watts))):
                out[k] = measured(v, "W", f"{self.METHOD_POWER} at {hz}", self.scope)
        else:
            out["mean_w"] = unavailable("power reading NOT_SUPPORTED", self.METHOD_POWER, self.scope)
        out["temp_max_c"] = (
            measured(float(max(temps)), "degC", self.METHOD_TEMP, self.scope)
            if temps
            else unavailable("temperature NOT_SUPPORTED", self.METHOD_TEMP, self.scope)
        )
        out["util_mean_pct"] = (
            measured(statistics.fmean(utils), "%", self.METHOD_UTIL, self.scope)
            if utils
            else unavailable("utilization NOT_SUPPORTED", self.METHOD_UTIL, self.scope)
        )
        return out


class ThermalSampler(_Polled):
    """acpitz zones (unlabelled on GX10, milli-degC) plus Processor cooling-device states.
    cpufreq is pinned by the performance governor and cannot show throttling; cur_state > 0
    on a Processor cooling device can. Negative states are a known driver bug and ignored."""

    scope = SCOPE_THERMAL
    METHOD_TEMP = "max over samples and zones of /sys/class/thermal/thermal_zone*/temp"
    METHOD_THROTTLE = "any Processor cooling_device*/cur_state > 0 during the window"

    def __init__(self, hz: float, root: Path = THERMAL_ROOT) -> None:
        super().__init__(hz)
        self.root = root
        self.zones: list[Path] = []
        self.coolers: list[Path] = []

    def _open(self) -> None:
        self.zones = sorted(p for p in self.root.glob("thermal_zone*") if (p / "temp").exists())
        self.coolers = sorted(
            p
            for p in self.root.glob("cooling_device*")
            if (p / "type").exists() and (p / "type").read_text().strip() == THROTTLE_COOLING_TYPE
        )
        if not self.zones:
            raise FileNotFoundError(f"no thermal zones under {self.root}")

    def sample(self) -> None:
        temps = [int((z / "temp").read_text()) / MILLI_C_PER_C for z in self.zones]
        states = [int((c / "cur_state").read_text()) for c in self.coolers]
        self.samples.append((time.monotonic(), temps, states))

    def _reduce(self) -> dict[str, Any]:
        peak = max(max(s[1]) for s in self.samples)
        throttled = any(st > 0 for s in self.samples for st in s[2])
        return {
            "max_temp_c": measured(peak, "degC", self.METHOD_TEMP, self.scope),
            "throttling_observed": measured(throttled, "bool", self.METHOD_THROTTLE, self.scope),
            "zones": len(self.zones),
            "cooling_devices": len(self.coolers),
        }


class CpuUtilSampler(_Polled):
    """Busy fraction from /proc/stat jiffies, whole machine and the pinned core; confirms the
    CPU tier really was a one-core load and the GPU tier was not secretly CPU-bound."""

    scope = SCOPE_CPU_UTIL
    METHOD = "/proc/stat busy jiffies / total jiffies, last sample - first sample"

    def __init__(self, hz: float, core: int | None, path: Path = PROC_STAT) -> None:
        super().__init__(hz)
        self.core = core
        self.path = path

    @staticmethod
    def _busy_total(fields: list[str]) -> tuple[int, int]:
        vals = [int(x) for x in fields[1:]]
        idle = vals[3] + vals[4]  # idle + iowait
        return sum(vals) - idle, sum(vals)

    def _open(self) -> None:
        self.path.read_text()

    def sample(self) -> None:
        rows = {ln.split()[0]: ln.split() for ln in self.path.read_text().splitlines() if ln.startswith("cpu")}
        core = rows.get(f"cpu{self.core}") if self.core is not None else None
        self.samples.append(
            (time.monotonic(), self._busy_total(rows["cpu"]), None if core is None else self._busy_total(core))
        )

    @staticmethod
    def _pct(a: tuple[int, int], b: tuple[int, int]) -> float | None:
        dt = b[1] - a[1]
        return None if dt <= 0 else 100.0 * (b[0] - a[0]) / dt

    def _reduce(self) -> dict[str, Any]:
        first, last = self.samples[0], self.samples[-1]
        out: dict[str, Any] = {}
        overall = self._pct(first[1], last[1])
        out["busy_pct_all_cores"] = (
            measured(overall, "%", self.METHOD, self.scope)
            if overall is not None
            else unavailable("window shorter than one jiffy", self.METHOD, self.scope)
        )
        if first[2] is not None and last[2] is not None:
            core = self._pct(first[2], last[2])
            out["busy_pct_pinned_core"] = (
                measured(core, "%", self.METHOD, f"cpu{self.core}")
                if core is not None
                else unavailable("window shorter than one jiffy", self.METHOD, f"cpu{self.core}")
            )
        else:
            out["busy_pct_pinned_core"] = unavailable("no core pinned", self.METHOD, self.scope)
        return out
