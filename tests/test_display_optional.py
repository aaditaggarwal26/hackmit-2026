"""Killing the dashboard cannot stop arbitration.

Four separate pieces of that property are already proven in isolation: a raising event sink costs
a log line and never a slot (test_fsm.py), a slow WebSocket client is dropped rather than waited
for (test_stream.py), telemetry is fire-and-forget UDP behind a bounded queue, and an unwritable
runs/ or a busy stream port only logs (orbit/ground/station.py). What was missing is the end of
the claim the demo actually makes: a whole run with *nothing* on the display side attached still
arbitrates to completion.

This drives the deterministic simulator rather than ``run_ground(stream_server=False,
telemetry_network=False)``. The simulator is that configuration -- it builds
``Telemetry(network=False)``, never constructs a ``StreamServer``, and with ``write_run=False``
gives the stream no writers -- and it runs under a virtual clock on the loopback bus, so the test
is fast and identical on every run. ``run_ground`` hard-wires its ``MulticastBus``, so reaching
the literal ``stream_server=False`` argument would mean real sockets and a real clock: slower,
flakier, and no stronger a statement about arbitration.
"""

import json

from orbit import config
from orbit.sim.run import run_scenario

S = config.Settings()
ROUNDS = 12
SEED = 7


def test_a_run_with_no_display_attached_still_grants_slots_and_ends_cleanly(tmp_path):
    headless = run_scenario("nominal", ROUNDS, seed=SEED, settings=S, write_run=False)

    # nothing downstream of the arbiter exists: no run file, no writers, no UDP, no stream socket
    assert headless.run_file is None
    assert headless.stream.writers == []
    assert headless.telemetry.network is False

    # and arbitration still ran to completion: slots were granted, won, and confirmed
    assert headless.ground.counters.completed >= ROUNDS
    assert headless.ground.counters.revoked == 0 and headless.ground.counters.failed_tx == 0
    completed = [r for r in headless.rows if r.outcome == "complete"]
    assert len(completed) >= ROUNDS
    assert {r.winner for r in completed} == set(S.sat_names())

    # the run closed itself normally rather than being cut short
    events = [json.loads(line) for line in headless.stream.lines]
    assert events[0]["type"] == "run_start"
    assert events[-1]["type"] == "run_end"
    assert sum(e["type"] == "run_end" for e in events) == 1

    # the observer really is an observer: attaching the display's consumers changes no decision
    watched = run_scenario("nominal", ROUNDS, seed=SEED, settings=S.with_overrides(runs_dir=str(tmp_path)))
    assert watched.run_file is not None and watched.stream.writers
    assert headless.digest() == watched.digest()
