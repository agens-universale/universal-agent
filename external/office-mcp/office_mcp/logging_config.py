from __future__ import annotations

import logging
import sys
from typing import Any

from pythonjsonlogger.json import JsonFormatter


def configure_logging(level: str = "INFO") -> None:
    handler = logging.StreamHandler(sys.stderr)
    formatter = JsonFormatter(
        "%(asctime)s %(levelname)s %(name)s %(message)s",
        rename_fields={"asctime": "ts", "levelname": "level"},
    )
    handler.setFormatter(formatter)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())


def mask_base64_in_args(args: dict[str, Any]) -> dict[str, Any]:
    masked = dict(args)
    if "content_base64" in masked and isinstance(masked["content_base64"], str):
        masked["content_base64"] = f"<base64: {len(masked['content_base64'])} bytes>"
    return masked
