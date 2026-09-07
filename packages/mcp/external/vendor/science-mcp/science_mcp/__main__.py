from __future__ import annotations

import argparse
import dataclasses
import logging
import os
import sys
from pathlib import Path

from science_mcp.config import load_config
from science_mcp.logging_config import configure_logging
from science_mcp.server import build_runtime, build_server

log = logging.getLogger(__name__)


def main() -> None:
    # Force UTF-8 on stdio so non-ASCII titles survive on Windows / cp1251 hosts.
    if hasattr(sys.stdin, "reconfigure"):
        sys.stdin.reconfigure(encoding="utf-8")
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")

    p = argparse.ArgumentParser(prog="science-mcp")
    p.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="Transport: http (default, daemon) or stdio (one-shot subprocess).",
    )
    p.add_argument(
        "--host",
        default=os.environ.get("SCIENCE_MCP_HOST", "127.0.0.1"),
        help="Bind host for http transport (default: env SCIENCE_MCP_HOST or 127.0.0.1).",
    )
    p.add_argument(
        "--port",
        type=int,
        default=int(os.environ.get("SCIENCE_MCP_PORT", "8765")),
        help="Bind port for http transport (default: env SCIENCE_MCP_PORT or 8765).",
    )
    p.add_argument(
        "--config",
        type=Path,
        default=None,
        help=(
            "Path to YAML config (primary source for mailto, api keys, "
            "cache, timeouts, host/port). Env vars used as fallback. "
            "If omitted, ./science-mcp.yaml or ~/.config/science-mcp.yaml is "
            "auto-loaded when present."
        ),
    )
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()
    configure_logging(args.log_level)

    # Override config host/port from CLI args before building runtime.
    cfg = load_config(yaml_path=args.config)
    if args.transport == "http":
        cfg = dataclasses.replace(cfg, host=args.host, port=args.port)

    rt = build_runtime(cfg)
    mcp = build_server(rt)

    transport_name = "streamable-http" if args.transport == "http" else "stdio"
    if args.transport == "http":
        log.info(
            "science-mcp listening on http://%s:%s/mcp",
            cfg.host, cfg.port,
        )
    else:
        log.info("science-mcp starting on stdio")
    mcp.run(transport=transport_name)


if __name__ == "__main__":
    main()
