"""Entry point gc-mcp-install → gc-mcp install (back-compat алиас)."""
from __future__ import annotations

import sys


def main() -> int:
    from mcp_runtime.cli import main as cli_main
    return cli_main(["install", *sys.argv[1:]])
