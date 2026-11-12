"""Прогресс загрузки MCP-серверов одного поколения шапки: thread-safe sink.

Поколение — сборка исполнителя. Статусы пишет приложение
(`TuiApp.settle_mcp_progress`) по снимку сервиса MCP — из рабочего потока
сборки и с тика шапки; читает TUI-поллинг. «Один активный run за раз» —
здесь (MVP)."""

from __future__ import annotations

import threading
from collections.abc import Iterable
from typing import Optional


class McpProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, Optional[dict]] = {}
        self._outcome: Optional[bool] = None
        self._built: Optional[tuple[frozenset[str], dict]] = None

    def begin(self, server_names) -> None:
        with self._lock:
            self._servers = dict.fromkeys(server_names)
            self._outcome = None
            self._built = None

    def mark_built(self, servers: Iterable[str], external: dict) -> None:
        """Сборка удалась: серверы поколения и статусы, которые дала сборка
        серверам вне сервиса. Поколение закрывается позже, когда серверы
        определятся (спека §5.7)."""
        with self._lock:
            self._built = (frozenset(servers), dict(external))

    def built(self) -> tuple[frozenset[str], dict] | None:
        """Итог удачной сборки (`mark_built`) или None — сборка не удалась
        или ещё идёт."""
        with self._lock:
            return self._built

    def finish(self, ok: bool) -> None:
        """Итог поколения: True — сборка удалась и серверы определились,
        False — сборка упала."""
        with self._lock:
            self._outcome = ok

    def update(self, server: str, status: dict) -> None:
        with self._lock:
            self._servers[server] = status

    def state(self) -> tuple[dict, Optional[bool]]:
        """Статусы серверов и итог сборки одним снимком: при итоге `True`
        в нём уже все статусы, которые успели прийти."""
        with self._lock:
            return dict(self._servers), self._outcome
