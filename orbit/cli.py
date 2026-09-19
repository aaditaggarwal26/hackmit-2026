"""`orbit`: one entry point for everything that runs on the ground station.

    orbit sim      --scenario memory_pressure --rounds 50 --seed 42   # deterministic table (Phase C)
    orbit ground   [--telemetry-host display.local]                    # the live arbiter on the multicast bus
    orbit sat      --profile sat-a [--scenario nominal]                # one simulated satellite, live
    orbit demo     [--scenario nominal] [--sats 3] [--display]         # ground + N sats (+ display) in one go
    orbit bench    --tier cpu,gpu                                      # energy/efficiency baseline
    orbit bus-smoke --listen | --send                                  # is multicast working on this network?
    orbit display  [--port 8765]                                       # the dashboard (normally on the laptop)

Any Settings field can be overridden with --set name=value or ORBIT_<NAME>=value.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import subprocess
import sys
import time
from dataclasses import fields
from typing import Any

from orbit import config, log
from orbit.config import Settings


def _apply_sets(s: Settings, sets: list[str]) -> Settings:
    kw: dict[str, Any] = {}
    types = {f.name: type(getattr(s, f.name)) for f in fields(s)}
    for item in sets:
        name, _, raw = item.partition("=")
        if name not in types:
            raise SystemExit(f"unknown setting {name!r}; known: {', '.join(sorted(types))}")
        kw[name] = config._coerce(raw, types[name])
    return s.with_overrides(**kw)


def _settings(a: argparse.Namespace) -> Settings:
    s = Settings.from_env()
    s = _apply_sets(s, a.set or [])
    if getattr(a, "telemetry_host", None):
        s = s.with_overrides(telemetry_host=a.telemetry_host)
    if getattr(a, "hostname", None):
        s = s.with_overrides(hostname=a.hostname)
    return s


def cmd_sim(a: argparse.Namespace) -> int:
    from orbit.sim.run import main
    argv = ["--scenario", a.scenario, "--rounds", str(a.rounds), "--seed", str(a.seed)]
    if a.digest:
        argv.append("--digest")
    if a.events:
        argv += ["--events", a.events]
    for k in ("item_aging_rate", "sat_aging_rate"):
        v = getattr(a, k)
        if v is not None:
            argv += [f"--{k.replace('_', '-')}", str(v)]
    for item in a.set or []:
        os.environ[config.ENV_PREFIX + item.partition("=")[0].upper()] = item.partition("=")[2]
    return main(argv)


def cmd_ground(a: argparse.Namespace) -> int:
    from orbit.ground.station import main_async
    asyncio.run(main_async(_settings(a), telemetry_network=not a.no_telemetry))
    return 0


def cmd_sat(a: argparse.Namespace) -> int:
    from orbit.sim.live import main_async, profile_for
    asyncio.run(main_async(profile_for(a.scenario, a.profile), _settings(a), a.seed))
    return 0


def cmd_demo(a: argparse.Namespace) -> int:
    """Ground + N satellites (+ display) as separate processes, like separate devices. Ctrl-C stops all."""
    base = [sys.executable, "-m", "orbit.cli"]
    passthrough = [x for item in (a.set or []) for x in ("--set", item)]
    procs: list[subprocess.Popen[bytes]] = []
    names = [f"sat-{chr(ord('a') + i)}" for i in range(a.sats)]
    try:
        if a.display:
            procs.append(subprocess.Popen([*base, "display", "--port", str(a.display_port)]))
            a.telemetry_host = a.telemetry_host or "127.0.0.1"
        ground = [*base, "ground", *passthrough]
        if a.telemetry_host:
            ground += ["--telemetry-host", a.telemetry_host]
        procs.append(subprocess.Popen(ground))
        time.sleep(0.5)
        for n in names:
            cmd = [*base, "sat", "--profile", n, "--scenario", a.scenario, "--seed", str(a.seed), *passthrough]
            procs.append(subprocess.Popen(cmd))
        where = f"{config.DEFAULTS.mcast_group}:{config.DEFAULTS.mcast_port}"
        disp = f" + display on http://127.0.0.1:{a.display_port}" if a.display else ""
        print(f"demo: ground + {names} on {where}{disp}. Ctrl-C to stop.", file=sys.stderr)
        while all(p.poll() is None for p in procs):
            time.sleep(0.5)
        dead = [str(p.args) for p in procs if p.poll() is not None]
        print(f"demo: a process exited ({dead}); stopping the rest", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        return 0
    finally:
        _stop_all(procs)


def _stop_all(procs: list[subprocess.Popen[bytes]]) -> None:
    """SIGINT everyone, wait briefly, kill stragglers. A second Ctrl-C during this skips straight to kill."""
    for p in procs:
        if p.poll() is None:
            p.send_signal(signal.SIGINT)
    try:
        for p in procs:
            try:
                p.wait(timeout=5)
            except subprocess.TimeoutExpired:
                p.kill()
    except KeyboardInterrupt:
        for p in procs:
            if p.poll() is None:
                p.kill()


def cmd_bench(a: argparse.Namespace) -> int:
    from orbit.bench.runner import main
    return main(a.rest)


def cmd_bus_smoke(a: argparse.Namespace) -> int:
    from tools.bus_smoke import main
    return main(a.rest)


def cmd_display(a: argparse.Namespace) -> int:
    from viz.server import main
    return main(["--port", str(a.port), "--telemetry-port", str(_settings(a).telemetry_port)])


def build_parser() -> argparse.ArgumentParser:
    ap = argparse.ArgumentParser(prog="orbit", description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log-level", default=os.environ.get("ORBIT_LOG_LEVEL", "INFO"))
    ap.add_argument("--set", action="append", metavar="NAME=VALUE", help="override any Settings field")
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("sim", help="deterministic simulation table")
    p.add_argument("--scenario", default="nominal")
    p.add_argument("--rounds", type=int, default=50)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--item-aging-rate", type=float, default=None)
    p.add_argument("--sat-aging-rate", type=float, default=None)
    p.add_argument("--events", default=None)
    p.add_argument("--digest", action="store_true")
    p.set_defaults(fn=cmd_sim)

    p = sub.add_parser("ground", help="live arbiter on the multicast bus")
    p.add_argument("--telemetry-host", default=None)
    p.add_argument("--hostname", default=None)
    p.add_argument("--no-telemetry", action="store_true")
    p.set_defaults(fn=cmd_ground)

    p = sub.add_parser("sat", help="one simulated satellite, live")
    p.add_argument("--profile", required=True, help="hostname, e.g. sat-a")
    p.add_argument("--scenario", default="nominal")
    p.add_argument("--seed", type=int, default=0)
    p.set_defaults(fn=cmd_sat)

    p = sub.add_parser("demo", help="ground + satellites (+ display) as processes")
    p.add_argument("--scenario", default="nominal")
    p.add_argument("--sats", type=int, default=3)
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--telemetry-host", default=None)
    p.add_argument("--display", action="store_true", help="also start the dashboard locally")
    p.add_argument("--display-port", type=int, default=8765)
    p.set_defaults(fn=cmd_demo)

    p = sub.add_parser("bench", help="energy/efficiency baseline (args passed through)")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_bench)

    p = sub.add_parser("bus-smoke", help="multicast reachability test (args passed through)")
    p.add_argument("rest", nargs=argparse.REMAINDER)
    p.set_defaults(fn=cmd_bus_smoke)

    p = sub.add_parser("display", help="dashboard server")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_display)
    return ap


def main(argv: list[str] | None = None) -> int:
    a = build_parser().parse_args(argv)
    log.setup(a.log_level)
    fn: Any = a.fn
    return int(fn(a))


if __name__ == "__main__":
    raise SystemExit(main())
