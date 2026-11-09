"""Прогресс загрузки MCP-серверов при сборке: thread-safe sink.

Sink наполняется из worker-потоков дискавери (через on_progress-колбэк),
читается TUI-поллингом. «Один активный run за раз» — здесь (MVP)."""

from __future__ import annotations

import threading
from typing import Optional


class McpProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, Optional[dict]] = {}
        self._outcome: Optional[bool] = None

    def begin(self, server_names) -> None:
        with self._lock:
            self._servers = dict.fromkeys(server_names)
            self._outcome = None

    def finish(self, ok: bool) -> None:
        """Сигнал конца сборки: True — завершена, False — упала."""
        with self._lock:
            self._outcome = ok

    def outcome(self) -> Optional[bool]:
        with self._lock:
            return self._outcome

    def update(self, server: str, status: dict) -> None:
        with self._lock:
            self._servers[server] = status

    def state(self) -> tuple[dict, Optional[bool]]:
        """Статусы серверов и итог сборки одним снимком: при итоге `True`
        в нём уже все статусы, которые успели прийти."""
        with self._lock:
            return dict(self._servers), self._outcome
