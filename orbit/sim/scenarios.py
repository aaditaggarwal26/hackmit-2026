"""Named constellations for the simulator. Each is a seedable, fully specified situation.

The point of a scenario is to make one question visible in a 50-round table:

* ``nominal``          three similar satellites, capture a little faster than the link drains —
                       the everyday contention the system is for.
* ``memory_pressure``  sat-c captures far faster than anything can drain it, so it evicts
                       constantly. Do the aging rates still let its good frames through, or is it
                       discarding them while it waits? The most acute form of starvation.
* ``low_scorer``       sat-b's frames consistently score lower (cloudier orbit, worse optics).
                       Item aging alone cannot rescue a satellite that never wins; satellite aging
                       must. Watch sat-b's sat_wait term climb until it takes a slot.
* ``revoke``           sat-c accepts grants and never transmits. The ground must time out, revoke,
                       re-arbitrate, and eventually raise a HARD flag on it — the one flag that
                       should mean "look at this".
* ``lossy``            the bus duplicates, reorders and drops datagrams. Nothing may crash; failed
                       transmissions must not lose frames.
* ``late_joiner``      sat-c comes online mid-pass: a fourth satellite needs zero configuration.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from orbit.bus.loopback import Faults
from orbit.sim.satellite import SatelliteProfile


@dataclass(frozen=True)
class Scenario:
    name: str
    description: str
    profiles: tuple[SatelliteProfile, ...]
    faults: Faults = field(default_factory=Faults)
    settings_overrides: dict[str, float | int] = field(default_factory=dict)


def _sat(host: str, seq: int, **kw: Any) -> SatelliteProfile:
    return SatelliteProfile(hostname=host, seq_seed=seq, **kw)


SCENARIOS: dict[str, Scenario] = {
    "nominal": Scenario(
        "nominal", "three similar satellites; capture slightly outpaces the link",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0),
         _sat("sat-c", 2, capture_period_s=4.0)),
    ),
    "memory_pressure": Scenario(
        "memory_pressure", "sat-c captures every 0.5 s into 8 slots: constant eviction",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0),
         _sat("sat-c", 2, capture_period_s=0.5)),
    ),
    "low_scorer": Scenario(
        "low_scorer", "sat-b scores 15 points lower than its peers on every frame",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0, score_bias=-15.0),
         _sat("sat-c", 2, capture_period_s=4.0)),
    ),
    "revoke": Scenario(
        "revoke", "sat-c is granted slots and never transmits",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0),
         _sat("sat-c", 2, capture_period_s=4.0, never_transmit=True)),
    ),
    "lossy": Scenario(
        "lossy", "bus duplicates 20%, reorders 10%, drops 2% of datagrams",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0),
         _sat("sat-c", 2, capture_period_s=4.0)),
        faults=Faults(duplicate=0.2, reorder=0.1, drop=0.02),
    ),
    "late_joiner": Scenario(
        "late_joiner", "sat-c boots 40 s into the pass with no configuration anywhere",
        (_sat("sat-a", 0, capture_period_s=4.0), _sat("sat-b", 1, capture_period_s=4.0),
         _sat("sat-c", 2, capture_period_s=4.0, boot_delay_s=40.0)),
    ),
}
