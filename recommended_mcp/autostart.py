"""Автозапуск MCP-daemon'ов по конфигу агента и манифесту recommended_mcp."""

from __future__ import annotations

from typing import Any

from mcp_runtime.manifest_resolve import recipe_root, resolve_agent_manifest_path
from recommended_mcp.manifest import load_manifest
from recommended_mcp.process_supervisor import RunSupervisor
from recommended_mcp.run import run_entry

# Транспорты, при которых агент ходит к уже запущенному серверу (не stdio-spawn).
_DAEMON_TRANSPORTS = frozenset({"http", "sse", "stream_http", "stream_sse"})


def ensure_daemons_for_agent(
    config_dict: dict[str, Any],
    *,
    manifest_path=None,
) -> RunSupervisor:
    """
    Поднять command-daemon'ы из манифеста, если они нужны включённым MCP в config агента.

    Идемпотентно: уже живые процессы не дублируются.
    Возвращает супервизор — в shutdown_managed() гасятся только процессы,
    запущенные в этом вызове.
    """
    path = manifest_path or resolve_agent_manifest_path(config_dict)
    if path is None:
        return RunSupervisor()

    recommended_root = recipe_root()
    manifest = load_manifest(path)
    servers = config_dict.get("mcp-servers") or {}
    supervisor = RunSupervisor()

    for entry in manifest:
        if entry.name not in servers:
            continue
        if entry.run is None or entry.run.type != "command":
            continue
        transport = str(servers[entry.name].get("transport", "")).strip()
        if transport not in _DAEMON_TRANSPORTS:
            continue
        run_entry(entry, recommended_root, supervisor)

    return supervisor
