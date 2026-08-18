"""Мост gc-cli → recommended_mcp: автозапуск HTTP/SSE MCP-daemon'ов."""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_supervisor: Any = None


def ensure_mcp_daemons(config_dict: dict[str, Any]) -> None:
    """Поднять недостающие MCP-daemon'ы перед запуском агента."""
    global _supervisor
    from gc_core.mcp_runtime.repo_path import ensure_repo_on_sys_path

    ensure_repo_on_sys_path()
    try:
        from recommended_mcp.autostart import ensure_daemons_for_agent
    except ImportError as exc:
        # Ловим ТОЛЬКО отсутствие самих рецептов. Раньше здесь стоял голый
        # `except ImportError: return`, который глотал и ошибку импорта ВНУТРИ
        # рецептов — опечатка там молча выключала автозапуск, а в gc-ua он
        # включён по умолчанию: агент шёл на мёртвый порт и падал позже, в
        # инструменте, вместо понятного сообщения.
        if getattr(exc, "name", None) != "recommended_mcp":
            raise
        logger.warning(
            "MCP-рецепты недоступны (нет пакета recommended_mcp) — HTTP/SSE "
            "daemon'ы не поднимаются; stdio-серверы из конфига работают как обычно"
        )
        return

    _supervisor = ensure_daemons_for_agent(config_dict)


def shutdown_mcp_daemons() -> None:
    """Остановить daemon'ы, поднятые ensure_mcp_daemons() в этом процессе."""
    global _supervisor
    if _supervisor is None:
        return
    try:
        _supervisor.shutdown_managed()
    finally:
        _supervisor = None
