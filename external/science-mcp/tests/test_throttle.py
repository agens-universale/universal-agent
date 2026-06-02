import asyncio
import time

import pytest

from science_mcp.errors import ScienceMCPError
from science_mcp.throttle import Throttler


@pytest.mark.asyncio
async def test_min_interval_serializes_calls():
    th = Throttler({"slow": 0.2})
    t0 = time.monotonic()
    await th.acquire("slow")
    await th.acquire("slow")
    await th.acquire("slow")
    elapsed = time.monotonic() - t0
    assert 0.35 < elapsed < 0.6


@pytest.mark.asyncio
async def test_independent_keys_parallel():
    th = Throttler({"a": 0.2, "b": 0.2})
    t0 = time.monotonic()
    await asyncio.gather(th.acquire("a"), th.acquire("b"))
    assert time.monotonic() - t0 < 0.1


@pytest.mark.asyncio
async def test_queue_timeout_raises_rate_limited():
    th = Throttler({"x": 0.2}, queue_timeout_sec=0.05)
    await th.acquire("x")
    with pytest.raises(ScienceMCPError) as ei:
        await th.acquire("x")
    assert ei.value.category.value == "rate_limited"


@pytest.mark.asyncio
async def test_set_interval_changes_pace():
    th = Throttler({"k": 0.5})
    th.set_interval("k", 0.0)
    t0 = time.monotonic()
    await th.acquire("k")
    await th.acquire("k")
    assert time.monotonic() - t0 < 0.05


@pytest.mark.asyncio
async def test_unknown_key_is_unthrottled():
    th = Throttler({})
    t0 = time.monotonic()
    await th.acquire("never_registered")
    assert time.monotonic() - t0 < 0.05
