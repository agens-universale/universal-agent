"""Единственное место tag-strip regex для for_model projection.

Семантика non-greedy (спека 08): для каждого тега — все closed-блоки, затем
один незакрытый хвост до EOF; по умолчанию финальный ``.strip()``.
"""

from __future__ import annotations

import re
from typing import Sequence


def strip_tagged_blocks(
    text: str, tags: Sequence[str], *, strip_outer_whitespace: bool = True
) -> str:
    """Удаляет inline ``<tag>…</tag>`` (non-greedy) и незакрытый ``<tag>`` до EOF."""
    if not isinstance(text, str) or not text:
        return ""
    result = text
    for tag in tags:
        name = str(tag or "").strip()
        if not name:
            continue
        escaped = re.escape(name)
        result = re.sub(
            rf"<{escaped}>.*?</{escaped}>",
            "",
            result,
            flags=re.DOTALL | re.IGNORECASE,
        )
        result = re.sub(
            rf"<{escaped}>.*",
            "",
            result,
            flags=re.DOTALL | re.IGNORECASE,
        )
    return result.strip() if strip_outer_whitespace else result


def strip_think_blocks(text: str) -> str:
    """Удаляет inline ``<think>…</think>`` (или незакрытый ``<think>`` до EOF)."""
    return strip_tagged_blocks(text, ("think",))
