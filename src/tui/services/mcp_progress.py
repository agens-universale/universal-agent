"""Прогресс загрузки MCP-серверов при старте: thread-safe sink + форматтеры.

Sink наполняется из worker-потоков дискавери (через on_progress-колбэк),
читается TUI-поллингом. «Один активный run за раз» — здесь (MVP)."""
from __future__ import annotations

import threading
from typing import Optional


class McpProgress:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._servers: dict[str, Optional[dict]] = {}

    def begin(self, server_names) -> None:
        with self._lock:
            self._servers = {name: None for name in server_names}

    def update(self, server: str, status: dict) -> None:
        with self._lock:
            self._servers[server] = status

    def snapshot(self) -> tuple[dict, bool]:
        with self._lock:
            return dict(self._servers), bool(self._servers)

    def all_reported(self) -> bool:
        with self._lock:
            return bool(self._servers) and all(v is not None for v in self._servers.values())


def _glyph(status: Optional[dict]) -> str:
    if status is None:
        return "⏳"
    return "✓" if status.get("status") == "connected" else "✗"


# Потолок видимых серверов в брендовом блоке: при превышении показываем
# первые (CAP-1) + строку-счётчик «… ещё +N», чтобы блок MCP не перерастал
# высоту бренда (все серверы всегда видны на экране /mcp).
_MAX_VISIBLE_SERVERS = 5


def format_mcp_progress(servers: dict) -> str:
    """Многострочный блок: заголовок + по строке на сервер (имя + статус-глиф).
    При >_MAX_VISIBLE_SERVERS список усекается до (CAP-1) строк + «… ещё +N»."""
    width = max((len(name) for name in servers), default=0)
    lines = ["подключаю MCP:"]
    items = list(servers.items())
    if len(items) > _MAX_VISIBLE_SERVERS:
        shown, hidden = items[: _MAX_VISIBLE_SERVERS - 1], len(items) - (_MAX_VISIBLE_SERVERS - 1)
        for name, st in shown:
            lines.append(f"{name.ljust(width)}  {_glyph(st)}")
        lines.append(f"… ещё +{hidden}")
    else:
        for name, st in items:
            lines.append(f"{name.ljust(width)}  {_glyph(st)}")
    return "\n".join(lines)


def format_mcp_settle(servers: dict) -> str:
    """Итог boot-проверки (остаётся записью в шапке): строка статуса MCP
    (предупреждение о недоступных либо «все подключены») + строка готовности."""
    failed = [name for name, st in servers.items()
              if not (st and st.get("status") == "connected")]
    if failed:
        word = "недоступен" if len(failed) == 1 else "недоступны"
        status = f"⚠ MCP: {', '.join(failed)} {word} — подробности в /mcp (F3)"
    else:
        status = "✓ MCP: все подключены"
    return f"{status}\n✓ готов к работе"
