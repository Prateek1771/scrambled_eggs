"""Cross-store consistency test (docs/06-benchmark.md metric 7): while a writer
commits a chain of supersessions, concurrent readers check whether they ever
observe a torn state. Arm-agnostic -- each arm supplies its own `supersede` and
`check_torn` callables (see bench/arm_a.py and bench/arm_b/consistency.py),
since what "torn" means is architecture-specific: for Arm A it's "both facts
current, or neither" inside one store; for Arm B it's "the stores disagree
about the same fact." This module only owns the concurrency and counting.

Run at concurrency 1, 10 and 50 per the doc's table.
"""

from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass
from typing import Awaitable, Callable

Supersede = Callable[[str, str], Awaitable[None]]
CheckTorn = Callable[[str, str], Awaitable[bool]]


@dataclass
class ConsistencyResult:
    concurrency: int
    torn_reads: int
    total_reads: int
    max_window_ms: float


async def _reader_loop(check_torn: CheckTorn, chain: list[str], stop: asyncio.Event,
                        counters: dict) -> None:
    while not stop.is_set():
        if len(chain) >= 2:
            old_id, new_id = chain[-2], chain[-1]
            started = time.perf_counter()
            torn = await check_torn(old_id, new_id)
            elapsed_ms = (time.perf_counter() - started) * 1000
            counters["total"] += 1
            if torn:
                counters["torn"] += 1
                counters["max_window_ms"] = max(counters["max_window_ms"], elapsed_ms)
        await asyncio.sleep(0)  # yield to the event loop; don't spin faster than the network allows


async def run_consistency_test(
    supersede: Supersede, check_torn: CheckTorn, seed_fact_id: str,
    concurrency: int, cycles: int = 50,
) -> ConsistencyResult:
    """`seed_fact_id` must already exist and be current (in every store) before
    this runs. Builds a chain of `cycles` supersessions, one after another, while
    `concurrency` readers poll the currently-being-written pair throughout."""
    chain = [seed_fact_id]
    stop = asyncio.Event()
    counters = {"total": 0, "torn": 0, "max_window_ms": 0.0}

    readers = [asyncio.create_task(_reader_loop(check_torn, chain, stop, counters))
               for _ in range(concurrency)]

    for i in range(cycles):
        old_id = chain[-1]
        new_id = f"{seed_fact_id}_c{i}"
        # Published before the write starts (and before it's necessarily
        # finished), so readers can catch the pair mid-write, not just after.
        chain.append(new_id)
        await supersede(old_id, new_id)

    stop.set()
    for reader in readers:
        await reader

    return ConsistencyResult(
        concurrency=concurrency, torn_reads=counters["torn"],
        total_reads=counters["total"], max_window_ms=counters["max_window_ms"],
    )
