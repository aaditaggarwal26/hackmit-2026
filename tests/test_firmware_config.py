"""Ground-side configuration the ESP32 firmware has to agree with.

Two things that cannot be checked by tools/check_firmware_sync.py, because neither is a
constant restated in orbit_config.h:

* the node roster the display pre-seeds, which has to carry the firmware's compile-time
  hostnames once the real boards are on the bus;
* the flash image's capture order, which has to be the order the simulator captures in,
  or the two tiers are scoring different imagery.
"""

import orbit.corpus as oc
from orbit.config import DEFAULTS, Settings
from orbit.sim.satellite import FakeSatellite, SatelliteProfile
from tools.build_fs_image import SAT_NODE_ID, capture_sequence


def test_sat_names_follows_nodes_real() -> None:
    assert DEFAULTS.sat_names() == ["sat-a", "sat-b", "sat-c"]
    # the firmware derives its hostname from the SAT_ID build flag; the ground matches it
    assert DEFAULTS.with_overrides(nodes_real=True).sat_names() == [
        "esp32-satellite-b",
        "esp32-satellite-c",
    ]
    # two boards exist, so the real roster lists two: a third would sit at ready=false all window
    assert len(DEFAULTS.with_overrides(nodes_real=True).sat_names()) == 2
    assert Settings.from_env({"ORBIT_NODES_REAL": "1"}).sat_names()[0] == "esp32-satellite-b"
    assert Settings.from_env({"ORBIT_EXPECTED_SATS": "a, b ,"}).sat_names() == ["a", "b"]


def test_flash_image_capture_order_matches_the_simulator() -> None:
    """The LittleFS image and FakeSatellite must capture the same frames in the same order.

    Both drop each scene's reference frame: the satellite carries it as a stored prior
    (/refs on the board) rather than capturing it again. Feeding references as captures
    hands the no-scoring FIFO baseline the corpus's cleanest frames for free.
    """
    corp = oc.load()
    for sat, node_id in SAT_NODE_ID.items():
        sim = FakeSatellite(SatelliteProfile(hostname=f"sat-{sat}", seq_seed=node_id), DEFAULTS, corp, 0)
        flashed = capture_sequence(corp, node_id, 0, 40)
        assert flashed == sim.sequence[:40], sat
        assert all(i != corp.reference_for(i) for i in flashed), sat
