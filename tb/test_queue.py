"""priority_queue against orbit.golden.queue.PriorityQueue: a long random mix of
insert / pop / set_limit, comparing head, count, evicted and lost_id after every
operation, plus the exact §5.3 rules on a 3-cell limit."""
import random

import cocotb
from cocotb.clock import Clock
from cocotb.triggers import FallingEdge

from orbit import params
from orbit.golden.queue import NO_FRAME, PriorityQueue

from orbit_tb import reset, run


async def setup(dut):
    cocotb.start_soon(Clock(dut.clk, 10, unit="ns").start())
    for s in ("insert", "in_score", "in_id", "pop", "set_limit", "new_limit"):
        getattr(dut, s).value = 0
    await reset(dut)


def state(dut):
    return (int(dut.has_data.value), int(dut.top_score.value), int(dut.top_id.value), int(dut.count.value),
            int(dut.evicted.value))


def model_state(q: PriorityQueue):
    return (int(q.has_data), q.top[0], q.top[1], len(q), q.evicted)


async def op_insert(dut, s, i):
    dut.in_score.value = s
    dut.in_id.value = i
    dut.insert.value = 1
    await FallingEdge(dut.clk)
    dut.insert.value = 0
    await FallingEdge(dut.clk)
    return int(dut.lost_id.value)


async def op_pop(dut):
    dut.pop.value = 1
    await FallingEdge(dut.clk)
    dut.pop.value = 0
    await FallingEdge(dut.clk)


async def op_limit(dut, n):
    dut.new_limit.value = n
    dut.set_limit.value = 1
    await FallingEdge(dut.clk)
    dut.set_limit.value = 0
    await FallingEdge(dut.clk)


@cocotb.test()
async def rules_on_three_cells(dut):
    await setup(dut)
    q = PriorityQueue(3)
    await op_limit(dut, 3)
    for s, i in [(5, 1), (9, 2), (5, 3)]:
        assert await op_insert(dut, s, i) == q.insert(s, i) == NO_FRAME
    assert state(dut) == model_state(q) and state(dut)[1:3] == (9, 2)
    assert await op_insert(dut, 4, 4) == q.insert(4, 4) == 4          # newcomer lost
    assert await op_insert(dut, 5, 5) == q.insert(5, 5) == 5          # equal to tail: newcomer lost
    assert await op_insert(dut, 7, 6) == q.insert(7, 6) == 3          # tail evicted
    assert state(dut) == model_state(q)
    await op_pop(dut); q.pop()
    assert state(dut) == model_state(q) and state(dut)[1:3] == (7, 6)
    await op_limit(dut, 1); q.set_limit(1)
    assert state(dut) == model_state(q) and state(dut)[3] == 1
    await op_pop(dut); q.pop()
    assert state(dut) == model_state(q) == (0, 0, NO_FRAME, 0, q.evicted)
    await op_pop(dut)                                                  # pop on empty: no change
    assert state(dut) == model_state(q)


@cocotb.test()
async def random_mix_vs_model(dut):
    await setup(dut)
    rng = random.Random(3)
    q = PriorityQueue(params.QUEUE_DEPTH)
    for step in range(600):
        r = rng.random()
        if r < 0.6:
            s, i = rng.choice([0, 65535, rng.randrange(65536), rng.randrange(8)]), rng.randrange(65535)
            lost = await op_insert(dut, s, i)
            assert lost == q.insert(s, i), f"step {step}: lost {lost} vs model"
        elif r < 0.9:
            await op_pop(dut)
            if q.has_data:
                q.pop()
        else:
            n = rng.randrange(1, params.QUEUE_DEPTH + 1)
            await op_limit(dut, n)
            q.set_limit(n)
        assert state(dut) == model_state(q), f"step {step}: {state(dut)} vs {model_state(q)}"
    assert q.evicted > 0


def test_priority_queue():
    run("priority_queue", ["priority_queue.v"], "test_queue")
