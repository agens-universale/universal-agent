"""Модель «глазка» загрузки MCP: по одному серверу за раз, по тикам.

Чистая логика без Textual: снимки загрузки (`sync`) превращаются в очередь
показа, `tick` двигает её; фаза и счётчик читаются экраном."""

from __future__ import annotations

from collections import deque
from collections.abc import Mapping
from typing import Literal

Phase = Literal["loading", "settled", "failed"]

HOLD_MAX_TICKS = 5
HOLD_BUDGET_TICKS = 20
SETTLE_TICKS = 10


class McpPeephole:
    def __init__(self, total: int = 0) -> None:
        """`total` — серверов в конфиге: фаза до первого снимка."""
        self._source: object = None
        self._generation = 0
        self._reset(total)

    def _reset(self, total: int) -> None:
        self._total = total
        self._queue: deque[tuple[str, bool]] = deque()
        self._queued: set[str] = set()
        self._connected = 0
        self._shown: tuple[str, bool] | None = None
        self._shown_count = 0
        self._age = 0
        self._hold = 0
        self._outcome: bool | None = None
        self._countdown = 0
        self._phase: Phase = "loading" if total > 0 else "settled"

    @property
    def phase(self) -> Phase:
        return self._phase

    @property
    def total(self) -> int:
        return self._total

    @property
    def connected(self) -> int:
        return self._connected

    @property
    def shown(self) -> tuple[str, bool] | None:
        return self._shown

    @property
    def generation(self) -> int:
        return self._generation

    @property
    def outcome(self) -> bool | None:
        """Конец сборки поколения: True — собрана, False — упала, None — идёт.
        Без серверов фаза `settled` наступает раньше него."""
        return self._outcome

    def finished(self, source: object) -> bool:
        """Поколение `source` дошло до итога или сбоя: новые снимки того же
        источника ничего не изменят."""
        return (
            source is self._source
            and self._outcome is not None
            and self._phase != "loading"
        )

    def sync(
        self,
        source: object,
        servers: Mapping[str, Mapping | None],
        outcome: bool | None,
    ) -> bool:
        """Принять снимок; True — изменилось то, что видно на экране.
        Число серверов задаёт поколение: список фиксируется до публикации."""
        before = self._view()
        if source is not self._source:
            self._source = source
            self._generation += 1
            self._reset(len(servers))
        if self._phase != "failed":
            self._enqueue_reported(servers)
            self._apply_outcome(servers, outcome)
        return self._view() != before

    def _view(self) -> tuple:
        return (
            self._generation,
            self._phase,
            self._outcome,
            self._total,
            self._connected,
            self._shown,
        )

    def _enqueue_reported(self, servers: Mapping[str, Mapping | None]) -> None:
        for name, status in servers.items():
            if status is not None and name not in self._queued:
                ok = status.get("status") == "connected"
                self._enqueue(name, ok)

    def _enqueue(self, name: str, ok: bool) -> None:
        self._queued.add(name)
        self._queue.append((name, ok))

    def _apply_outcome(
        self, servers: Mapping[str, Mapping | None], outcome: bool | None
    ) -> None:
        if outcome is False:
            self._phase = "failed"
            self._queue.clear()
            self._shown = None
        elif outcome is True and self._outcome is None:
            for name in servers:
                if name not in self._queued:
                    self._enqueue(name, False)
        if outcome is not None:
            self._outcome = outcome

    def tick(self) -> None:
        if self._phase != "loading":
            return
        if self._shown is not None:
            self._age += 1
        if self._shown is None or self._age > self._hold:
            self._take_next()
        if self._can_settle():
            self._countdown += 1
            if self._countdown >= SETTLE_TICKS:
                self._phase = "settled"
                self._shown = None

    def _take_next(self) -> None:
        if not self._queue:
            return
        queue_len = len(self._queue)
        name, ok = self._queue.popleft()
        self._shown = (name, ok)
        self._shown_count += 1
        self._age = 1
        self._hold = max(
            1, min(HOLD_MAX_TICKS, round(HOLD_BUDGET_TICKS / queue_len))
        )
        if ok:
            self._connected += 1

    def _can_settle(self) -> bool:
        """Все показаны, последний hold истёк, сборка завершена успешно."""
        if self._outcome is not True or self._shown_count < self._total:
            return False
        return self._shown is None or self._age > self._hold
