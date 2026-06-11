"""Автозапуск MCP-daemon'ов по конфигу агента и манифесту recommended_mcp."""

from __future__ import annotations

import socket
import time
from typing import Any
from urllib.parse import urlparse

from mcp_runtime.manifest_resolve import recipe_root, resolve_agent_manifest_path
from recommended_mcp.manifest import load_manifest
from recommended_mcp.process_supervisor import RunSupervisor
from recommended_mcp.run import run_entry

# Транспорты, при которых агент ходит к уже запущенному серверу (не stdio-spawn).
_DAEMON_TRANSPORTS = frozenset({"http", "sse", "stream_http", "stream_sse"})

_DEFAULT_READY_TIMEOUT_SEC = 15.0
_READY_POLL_SEC = 0.05


def _daemon_endpoint(url: str) -> tuple[str, int]:
    parsed = urlparse(url)
    host = parsed.hostname or "127.0.0.1"
    if parsed.port is not None:
        return host, parsed.port
    if parsed.scheme == "https":
        return host, 443
    return host, 80


def wait_daemon_url_ready(
    url: str,
    *,
    timeout: float = _DEFAULT_READY_TIMEOUT_SEC,
    poll: float = _READY_POLL_SEC,
) -> bool:
    """Дождаться, пока TCP-порт MCP-daemon'а начнёт принимать соединения."""
    host, port = _daemon_endpoint(url)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(poll)
    return False


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
        server_cfg = servers[entry.name]
        transport = str(server_cfg.get("transport", "")).strip()
        if transport not in _DAEMON_TRANSPORTS:
            continue
        started = run_entry(entry, recommended_root, supervisor)
        if not started:
            continue
        url = server_cfg.get("url")
        if not url:
            continue
        print(f"[wait] {entry.name}: {url}")
        if wait_daemon_url_ready(str(url)):
            print(f"[ready] {entry.name}: {url}")
        else:
            print(
                f"[warn] {entry.name}: daemon not accepting connections at {url} "
                f"after {_DEFAULT_READY_TIMEOUT_SEC:.0f}s"
            )

    return supervisor
