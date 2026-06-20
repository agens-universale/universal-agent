"""Вспомогательные функции для извлечения markdown из результата AgentExecutor."""
from __future__ import annotations

import re
from typing import Any


_AGENT_ERROR_PREFIXES = (
    "Agent executed ",
    "Agent tools call errors.",
)


def _strip_code_fence(text: str) -> str:
    """Strip a wrapping ```[lang]\\n...\\n``` fence. Returns inner content or original."""
    stripped = text.strip()
    m = re.match(r"^```[\w]*\n(.+)\n```$", stripped, re.DOTALL)
    if m:
        return m.group(1)
    return stripped


def extract_markdown(agent_result: Any) -> str:
    """Extract markdown from AgentExecutor.run() result.

    Fallback chain:
      (a) task_report field from finish_arguments JSON
      (b) handoff field from finish_arguments JSON
      (c) raw text response (text_response mode)
    Strips wrapping code fences.
    Returns "" for None, empty, or known agent error strings.
    """
    import json as _json

    if not agent_result:
        return ""

    raw = str(agent_result).strip()
    if not raw:
        return ""

    try:
        parsed = _json.loads(raw)
    except (_json.JSONDecodeError, TypeError):
        parsed = None

    if isinstance(parsed, dict):
        for key in ("task_report", "handoff"):
            value = parsed.get(key)
            if isinstance(value, str) and value.strip():
                return _strip_code_fence(value)
        return ""

    if any(raw.startswith(prefix) for prefix in _AGENT_ERROR_PREFIXES):
        return ""

    return _strip_code_fence(raw)
