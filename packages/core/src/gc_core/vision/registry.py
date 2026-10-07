"""Process-wide results and cross-loop coordination of visual probes."""

import asyncio
import threading
import time
from collections.abc import Awaitable, Callable
from concurrent.futures import Future
from dataclasses import dataclass, field

from gc_core.vision.probe import ProbeResult, ProbeStatus
from gc_core.vision.route import VisionRouteKey


@dataclass
class _Entry:
    result: ProbeResult | None = None
    deadline: float = 0
    turns: set[str] = field(default_factory=set)
    pending: Future | None = None


class VisionProbeRegistry:
    def __init__(self, clock: Callable[[], float] = time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._entries: dict[VisionRouteKey, _Entry] = {}

    def peek(self, key: VisionRouteKey) -> ProbeResult | None:
        with self._lock:
            entry = self._entries.get(key)
            return entry.result if entry else None

    def can_probe(self, key, *, turn_key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.result is None:
                return True
            return (
                entry.result.status is ProbeStatus.TRANSIENT
                and turn_key not in entry.turns
                and self._clock() >= entry.deadline
            )

    def mark_unsupported(self, key: VisionRouteKey, *, reason: str) -> bool:
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            changed = (
                entry.result is None
                or entry.result.status is not ProbeStatus.UNSUPPORTED
            )
            entry.result = ProbeResult(ProbeStatus.UNSUPPORTED, reason)
            entry.turns.clear()
            return changed

    def _take(self, key, turn_key):
        with self._lock:
            entry = self._entries.setdefault(key, _Entry())
            result = entry.result
            if result is not None:
                if result.status is not ProbeStatus.TRANSIENT:
                    return result, None, False
                forbidden = (
                    turn_key in entry.turns or self._clock() < entry.deadline
                )
                entry.turns.add(turn_key)
                if forbidden:
                    return result, None, False
            else:
                entry.turns.add(turn_key)
            if entry.pending is not None:
                return None, entry.pending, False
            entry.pending = Future()
            return None, entry.pending, True

    def _finish(self, key, future, result, *, cancelled=False):
        with self._lock:
            entry = self._entries[key]
            entry.pending = None
            if cancelled:
                # Cancellation adds no verdict and must not erase an earlier
                # network failure or allow its root turn to probe again.
                if entry.result is None:
                    del self._entries[key]
            elif (
                entry.result is not None
                and entry.result.status is ProbeStatus.UNSUPPORTED
            ):
                result = entry.result
            else:
                entry.result = result
                if result.status is ProbeStatus.TRANSIENT:
                    entry.deadline = self._clock() + max(
                        30, result.retry_after_sec
                    )
                else:
                    entry.turns.clear()
        # Future callbacks can acquire locks or schedule work in another loop.
        future.set_result(result)
        return result

    async def ensure(
        self, key, *, turn_key: str, run: Callable[[], Awaitable[ProbeResult]]
    ) -> ProbeResult:
        cached, future, owner = self._take(key, turn_key)
        if cached is not None:
            return cached
        if not owner:
            return await asyncio.shield(asyncio.wrap_future(future))
        try:
            result = await run()
        except BaseException:
            self._finish(
                key,
                future,
                ProbeResult(ProbeStatus.TRANSIENT, "probe_cancelled"),
                cancelled=True,
            )
            raise
        return self._finish(key, future, result)


_PROBES = VisionProbeRegistry()


def process_vision_probes() -> VisionProbeRegistry:
    return _PROBES


def reset_vision_probes_for_tests() -> None:
    global _PROBES
    _PROBES = VisionProbeRegistry()
