"""Мост gc-cli → recommended_mcp: автозапуск HTTP/SSE MCP-daemon'ов."""

from __future__ import annotations

from typing import Any

_supervisor: Any = None


def ensure_mcp_daemons(config_dict: dict[str, Any]) -> None:
    """Поднять недостающие MCP-daemon'ы перед запуском агента."""
    global _supervisor
    from gc_core.mcp_runtime.repo_path import ensure_repo_on_sys_path

    ensure_repo_on_sys_path()
    try:
        from recommended_mcp.autostart import ensure_daemons_for_agent
    except ImportError:
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
