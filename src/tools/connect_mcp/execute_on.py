"""Pick the MCP client for one server: locus + backend kind -> implementation.

The single place that knows the mapping. Neither client implementation is aware
of the other, and no transport helper is aware of the locus.
"""
import logging
from typing import Any, Dict, Optional

from tools.connect_mcp.client_port import AgentRuntimeClient, McpClientPort

logger = logging.getLogger(__name__)

# Transports whose handshake plus one RPC fit in a single request/response and
# therefore survive a stateless exec round-trip. The rest need a live session
# (a long-lived stream or a held process), which a one-shot exec cannot give.
OS_BACKEND_TRANSPORTS = ("http", "stream_http", "stdio")

_LOCI = ("agent", "os_backend")


class McpExecuteOnError(ValueError):
    """Configuration cannot be honoured at the requested locus."""


def resolve_mcp_client(
    server_name: str,
    server_cfg: Dict[str, Any],
    *,
    os_backend: Optional[Any] = None,
) -> McpClientPort:
    cfg = server_cfg if isinstance(server_cfg, dict) else {}
    locus = str(cfg.get("execute_on") or "agent")
    if locus not in _LOCI:
        raise McpExecuteOnError(
            f"MCP server '{server_name}': unknown execute_on {locus!r} "
            f"(expected one of {', '.join(_LOCI)})"
        )

    if locus == "agent":
        return AgentRuntimeClient(server_name, cfg)

    # Locus is os_backend from here on.
    if os_backend is None or not getattr(os_backend, "is_sandbox", False):
        # Same filesystem, same network, same PATH as the agent — routing through
        # exec would buy nothing and only add a shell round-trip. Not an error:
        # one config is meant to work in both modes.
        logger.debug(
            "MCP server '%s': execute_on=os_backend on a non-sandbox backend, "
            "running in the agent runtime",
            server_name,
        )
        return AgentRuntimeClient(server_name, cfg)

    transport = str(cfg.get("transport") or "")
    if transport not in OS_BACKEND_TRANSPORTS:
        raise McpExecuteOnError(
            f"MCP server '{server_name}': transport {transport!r} needs a live "
            f"session and cannot run on os_backend. Supported there: "
            f"{', '.join(OS_BACKEND_TRANSPORTS)}. Set execute_on: agent to run it "
            f"in the agent runtime instead."
        )

    # Nothing is copied into the environment, so anything that resolves to a file
    # path there cannot work. A bare `verify-ssl: false` is just a flag and is fine.
    cert = cfg.get("client-cert") or cfg.get("client_cert")
    key = cfg.get("client-key") or cfg.get("client_key")
    verify = cfg.get("verify-ssl", cfg.get("verify_ssl", True))
    if cert or key or isinstance(verify, str):
        raise McpExecuteOnError(
            f"MCP server '{server_name}': client-cert / client-key / a CA path in "
            f"verify-ssl need a file inside the environment, and execute_on: "
            f"os_backend copies no files there. Use verify-ssl: false, or set "
            f"execute_on: agent."
        )

    from tools.connect_mcp.os_backend_client import OsBackendStatelessClient

    return OsBackendStatelessClient(server_name, cfg, os_backend=os_backend)


__all__ = ["McpExecuteOnError", "OS_BACKEND_TRANSPORTS", "resolve_mcp_client"]
