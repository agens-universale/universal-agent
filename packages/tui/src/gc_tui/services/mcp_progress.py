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
            self._servers = dict.fromkeys(server_names)

    def update(self, server: str, status: dict) -> None:
        with self._lock:
            self._servers[server] = status

    def snapshot(self) -> tuple[dict, bool]:
        with self._lock:
            return dict(self._servers), bool(self._servers)

    def all_reported(self) -> bool:
        with self._lock:
            return bool(self._servers) and all(
                v is not None for v in self._servers.values()
            )


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
    lines = [
        "подключаю MCP:",
        "",
    ]  # заголовок + пустая строка-отступ перед списком
    items = list(servers.items())
    if len(items) > _MAX_VISIBLE_SERVERS:
        shown, hidden = (
            items[: _MAX_VISIBLE_SERVERS - 1],
            len(items) - (_MAX_VISIBLE_SERVERS - 1),
        )
        for name, st in shown:
            lines.append(f"{name.ljust(width)}  {_glyph(st)}")
        lines.append(f"… ещё +{hidden}")
    else:
        for name, st in items:
            lines.append(f"{name.ljust(width)}  {_glyph(st)}")
    return "\n".join(lines)


def _server_ok(st) -> bool:
    # Статус сервера: {"status", "reason", "tool_count", "elapsed_ms"};
    # OK ⟺ status == "connected" (иначе "failed" или None=ещё коннектится).
    return isinstance(st, dict) and st.get("status") == "connected"


def format_mcp_settle(servers: dict) -> str:
    """Итог boot-проверки (строка MCP в карточке): статус по реальному состоянию
    (✓ все / ⚠ N/M) + хинт «/mcp». Глиф отделён пробелом («⚠ MCP»).

    Строки уведомлений и готовности сюда НЕ добавляем: их собирает экран
    (_compose_card_lines) по осевшему mcp_status. См. main.py::_settle_mcp_row."""
    total = len(servers)
    connected = sum(1 for st in servers.values() if _server_ok(st))
    if connected == total:
        return "✓ MCP: все подключены · /mcp"
    if connected == 0:
        return f"⚠ MCP: 0/{total} — ни один не подключился · /mcp"
    return f"⚠ MCP: {connected}/{total} подключены · /mcp"
