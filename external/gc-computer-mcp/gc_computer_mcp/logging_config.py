"""Structured JSON logging to stderr with secret masking helpers."""

from __future__ import annotations

import logging
import sys
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from pythonjsonlogger.json import JsonFormatter


def configure_logging(level: str = "INFO", log_dir: Path | None = None) -> None:
    root = logging.getLogger()
    root.setLevel(level)
    root.handlers.clear()

    fmt = JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level"},
    )

    stderr_handler = logging.StreamHandler(sys.stderr)
    stderr_handler.setFormatter(fmt)
    root.addHandler(stderr_handler)

    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        file_handler = logging.FileHandler(
            log_dir / "gc-computer-mcp.log", encoding="utf-8",
        )
        file_handler.setFormatter(fmt)
        root.addHandler(file_handler)


def mask_type_args(args: Mapping[str, Any]) -> dict[str, Any]:
    """Replace sensitive 'text' arg with its length (for INFO logs of 'type' tool)."""
    masked = dict(args)
    if "text" in masked:
        text = masked.pop("text")
        masked["text_length"] = len(text) if isinstance(text, str) else 0
    return masked
