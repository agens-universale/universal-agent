"""Одна запись SSE: ``event``, ``data``, пустая строка."""

from __future__ import annotations

import json

_TERMINAL = frozenset(
    {
        "response.completed",
        "response.incomplete",
        "response.cancelled",
        "response.failed",
    }
)


def encode_sse(event: dict) -> bytes:
    payload = json.dumps(event, ensure_ascii=False, separators=(",", ":"))
    text = "event: " + event["type"] + "\ndata: " + payload + "\n\n"
    return text.encode("utf-8")


def is_terminal(event: dict) -> bool:
    return event.get("type") in _TERMINAL
