"""Единственное место think-regex для for_model projection."""

from __future__ import annotations

import re

# Greedy: first <think> → LAST </think> (one span). Паритет с
# tui.services.trace_mapper._THINK_BLOCK_RE. Вторая ветка — незакрытый тег до EOF.
_THINK_BLOCK_RE = re.compile(r"<think>.*</think>|<think>.*", re.DOTALL | re.IGNORECASE)


def strip_think_blocks(text: str) -> str:
    """Удаляет inline ``<think>…</think>`` (или незакрытый ``<think>`` до EOF)."""
    if not isinstance(text, str) or not text:
        return ""
    return _THINK_BLOCK_RE.sub("", text).strip()
