"""CLI entry point.

Usage:
    computer-mcp                 # stdio transport, config from env
    computer-mcp --version
    computer-mcp --readonly      # forces readonly regardless of env
    computer-mcp --enable-vision # forces vision on regardless of env

Transport is always stdio in v0.1 (HTTP transport deferred).
"""

from __future__ import annotations

import argparse
import dataclasses
import sys

from . import __version__
from .config import load_from_env
from .core.dpi import setup_dpi_awareness
from .logging_config import configure_logging
from .server import build_server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="computer-mcp",
        description="MCP server for Windows GUI automation",
    )
    parser.add_argument(
        "--version", action="version", version=f"%(prog)s {__version__}",
    )
    parser.add_argument(
        "--readonly", action="store_true",
        help="force readonly mode (overrides env)",
    )
    parser.add_argument(
        "--enable-vision", action="store_true",
        help="force enable vision tools (overrides env)",
    )
    parser.add_argument(
        "--log-level", default=None,
        help="override log level (DEBUG/INFO/WARNING/ERROR/CRITICAL)",
    )
    parser.add_argument(
        "--debug-ancestors", action="store_true",
        help="print process ancestor chain and exit (diagnostic for session_id)",
    )
    args = parser.parse_args(argv)

    if args.debug_ancestors:
        _debug_ancestors()
        return 0

    cfg = load_from_env()
    overrides: dict = {}
    if args.readonly:
        overrides["readonly"] = True
    if args.enable_vision:
        overrides["enable_vision"] = True
    if args.log_level:
        overrides["log_level"] = args.log_level.upper()
    if overrides:
        cfg = dataclasses.replace(cfg, **overrides)

    configure_logging(level=cfg.log_level, log_dir=cfg.log_dir)
    setup_dpi_awareness()

    mcp = build_server(cfg)
    mcp.run(transport="stdio")
    return 0


def _debug_ancestors() -> None:
    """Print this process's ancestor chain. Used to diagnose session_id resolution."""
    import os

    print(f"os.getpid(): {os.getpid()}")
    print(f"os.getppid(): {os.getppid()}")
    try:
        import psutil
    except ImportError:
        print("psutil not installed")
        return

    try:
        p = psutil.Process()
        print(f"self: pid={p.pid} name={p.name()!r} exe={p.exe()!r}")
    except Exception as e:  # noqa: BLE001
        print(f"self lookup failed: {e}")
        return

    parent = p.parent()
    depth = 0
    while parent is not None and depth < 12:
        try:
            print(f"  ancestor[{depth}]: pid={parent.pid} name={parent.name()!r}")
        except Exception as e:  # noqa: BLE001
            print(f"  ancestor[{depth}]: error reading name: {e}")
            break
        try:
            parent = parent.parent()
        except Exception as e:  # noqa: BLE001
            print(f"  ancestor walk failed at depth {depth}: {e}")
            break
        depth += 1

    from .core.state_store import _resolve_default_session_id

    print(f"resolved session_id: {_resolve_default_session_id()!r}")


if __name__ == "__main__":
    sys.exit(main())
