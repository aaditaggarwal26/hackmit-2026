"""Fixed-point helpers matching the RTL bit for bit. Everything is int64 NumPy
or Python int; there is no float anywhere in the node model."""
import math

import numpy as np

from orbit import params

FRAC = params.Q_FRAC_BITS           # 8
ONE = 1 << FRAC                     # 256 units per metre
ACC_MOD = 1 << params.ACC_BITS      # 2^48
ACC_HALF = 1 << (params.ACC_BITS - 1)
I32_MOD = 1 << 32
I32_HALF = 1 << 31
Q_MIN, Q_MAX = -ACC_HALF, ACC_HALF - 1


def wrap48(v):
    """Two's-complement wrap to 48 bits (the accumulators never saturate)."""
    return ((v + ACC_HALF) % ACC_MOD) - ACC_HALF


def wrap32(v):
    return ((v + I32_HALF) % I32_MOD) - I32_HALF


def ashr8(v):
    """Q40.8 -> integer metres. Arithmetic shift = floor. Verilog `>>>`.
    NumPy `>>` on int64 is arithmetic; Python `>>` on int is floor. Both match."""
    return v >> FRAC


def to_q(metres: float) -> int:
    """Orchestrator-side conversion: round to nearest 1/256 m. Asserts range
    rather than saturating: a state vector that does not fit is a bug upstream."""
    q = int(round(metres * ONE))
    if not Q_MIN <= q <= Q_MAX:
        raise OverflowError(f"{metres} m does not fit Q40.8")
    return q


def r0_to_q(r0_m: int) -> int:
    """int32 metres -> Q40.8, exactly what the node does on STATE_PUSH."""
    return int(r0_m) << FRAC


def as_i64(a):
    return np.asarray(a, dtype=np.int64)


# --- additions after golden-v1 (additive only; nothing above changed) -----------
def wrap(value, bits: int):
    """Generic two's-complement wrap to `bits` bits. wrap48 and wrap32 are the
    two instances the datapath uses; they are kept verbatim above.
    Python ints: any width. int64 NumPy arrays: the datapath widths (32, 48)
    are tested; in general value + 2^(bits-1) must fit int64, so bits <= 62."""
    half = 1 << (bits - 1)
    return ((value + half) % (1 << bits)) - half


def sat(value, bits: int):
    """Saturating clamp to the signed `bits`-bit range. The node's datapath
    never saturates: accumulators and the delta wrap (protocol.md §5.1), and
    the only range check anywhere is the orchestrator-side to_q, which raises.
    sat() exists for tests and future use, not for modelling the RTL."""
    lo, hi = -(1 << (bits - 1)), (1 << (bits - 1)) - 1
    if isinstance(value, np.ndarray):
        return np.clip(value, lo, hi)
    return min(max(int(value), lo), hi)


def q408_from_m(metres: float) -> int:
    """metres -> Q40.8 with round-half-AWAY-FROM-ZERO (the C/Verilog-testbench
    convention). Differs from to_q (Python round = banker's, half to even)
    ONLY when metres*256 lands exactly on .5, i.e. odd multiples of 1/512 m:
    +0.001953125 m -> 1 here, 0 in to_q; -0.001953125 m -> -1 here, 0 in to_q.
    Same range check as to_q so the two agree everywhere else."""
    x = metres * ONE
    t = math.trunc(x)
    f = x - t                      # exact in binary for |x| < 2^52; no floor(|x|+0.5), which misrounds 0.5-2^-54
    q = t + (1 if f >= 0.5 else -1 if f <= -0.5 else 0)
    if not Q_MIN <= q <= Q_MAX:
        raise OverflowError(f"{metres} m does not fit Q40.8")
    return q


def q408_to_m_trunc(q):
    """Q40.8 -> integer metres by arithmetic shift right 8 (floor toward -inf,
    Verilog `>>>`). Alias of ashr8: -1 -> -1, -256 -> -1, -257 -> -2."""
    return ashr8(q)
