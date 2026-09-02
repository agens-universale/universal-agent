from __future__ import annotations

import asyncio
from typing import Optional

from science_mcp.errors import ErrorCategory, ScienceMCPError

DEFAULT_MIN_INTERVALS: dict[str, float] = {
    "openalex": 0.1,
    "ncbi": 0.34,
    "crossref": 0.035,
    "arxiv": 3.0,
    "semantic_scholar": 1.0,
    "dblp": 1.0,
    "core": 2.0,
    "opencitations": 0.34,
    "biorxiv": 1.0,
}


class Throttler:
    def __init__(
        self,
        min_intervals: Optional[dict[str, float]] = None,
        *,
        queue_timeout_sec: float = 120.0,
    ):
        self._intervals: dict[str, float] = dict(
            min_intervals if min_intervals is not None else DEFAULT_MIN_INTERVALS
        )
        self._locks: dict[str, asyncio.Lock] = {}
        self._last_call: dict[str, float] = {}
        self._queue_timeout = queue_timeout_sec

    def set_interval(self, key: str, seconds: float) -> None:
        self._intervals[key] = seconds

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    async def acquire(self, key: str) -> None:
        interval = self._intervals.get(key, 0.0)
        try:
            await asyncio.wait_for(self._guarded(key, interval), timeout=self._queue_timeout)
        except asyncio.TimeoutError:
            raise ScienceMCPError(
                ErrorCategory.RATE_LIMITED,
                source=key,
                retry_after_sec=int(interval),
            )

    async def _guarded(self, key: str, interval: float) -> None:
        async with self._lock(key):
            loop = asyncio.get_running_loop()
            now = loop.time()
            last = self._last_call.get(key, 0.0)
            wait = (last + interval) - now
            if wait > 0:
                await asyncio.sleep(wait)
            self._last_call[key] = loop.time()
