from __future__ import annotations

import argparse
import logging
from pathlib import Path

import uvicorn

from .logging_config import configure_logging
from .server import ServerConfig, build_asgi_app, build_server

log = logging.getLogger(__name__)


def _parse_size(s: str) -> int:
    s = s.strip().upper()
    mult = 1
    if s.endswith("MB"):
        mult, s = 1024 * 1024, s[:-2]
    elif s.endswith("KB"):
        mult, s = 1024, s[:-2]
    elif s.endswith("B"):
        s = s[:-1]
    return int(float(s) * mult)


def main() -> None:
    p = argparse.ArgumentParser(prog="mcp-office-reader")
    p.add_argument(
        "--transport",
        choices=["http", "stdio"],
        default="http",
        help="Transport type: http (default) or stdio.",
    )
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8080)
    p.add_argument(
        "--allowed-dirs",
        nargs="*",
        default=None,
        help="Directories from which read_file may read. Defaults to cwd.",
    )
    p.add_argument(
        "--allow-any",
        action="store_true",
        help="Disable path whitelist (use with care, e.g. inside a locked-down container).",
    )
    p.add_argument("--max-file-size", default="50MB")
    p.add_argument("--max-output-chars", type=int, default=1_000_000)
    p.add_argument("--timeout-seconds", type=int, default=60)
    p.add_argument("--log-level", default="INFO")
    p.add_argument(
        "--max-output-base64-size",
        default="20MB",
        help="Max encoded size for base64-returning create_* tools.",
    )
    p.add_argument(
        "--output-allowed-dirs",
        nargs="*",
        default=None,
        help="Directories where create_*_file may write. Defaults to --allowed-dirs.",
    )
    p.add_argument(
        "--max-query-rows",
        type=int,
        default=10000,
        help="Max rows returned inline by analytics tools (use output={path} for larger results).",
    )
    args = p.parse_args()

    configure_logging(args.log_level)
    allowed = args.allowed_dirs or [str(Path.cwd())]
    if args.allow_any:
        log.warning("whitelist disabled via --allow-any", extra={"event": "startup"})

    cfg = ServerConfig(
        allowed_dirs=allowed,
        allow_any=args.allow_any,
        max_file_size=_parse_size(args.max_file_size),
        max_output_chars=args.max_output_chars,
        timeout_s=args.timeout_seconds,
        max_output_base64_size=_parse_size(args.max_output_base64_size),
        output_allowed_dirs=args.output_allowed_dirs or allowed,
        max_query_rows=args.max_query_rows,
    )
    if args.transport == "stdio":
        mcp = build_server(cfg)
        mcp.run(transport="stdio")
    else:
        app = build_asgi_app(cfg)
        uvicorn.run(app, host=args.host, port=args.port, log_config=None)


if __name__ == "__main__":
    main()
