"""INA219 raw registers -> physical units. The one place this arithmetic lives
(protocol.md §4.11: raw registers on the wire, conversion in Python).
Used by the energy sampler and the dashboard."""
from orbit import params


def convert(bus_raw: int, shunt_raw: int) -> dict:
    """bus_raw: reg 0x02 verbatim (u16); shunt_raw: reg 0x01 verbatim (i16).
    Returns volts, shunt_v, amps, watts."""
    bus_v = (bus_raw >> 3) * params.INA219_BUS_LSB_V
    shunt_v = shunt_raw * params.INA219_SHUNT_LSB_V
    amps = shunt_v / params.INA219_SHUNT_OHM
    return dict(volts=bus_v, shunt_v=shunt_v, amps=amps, watts=bus_v * amps)
