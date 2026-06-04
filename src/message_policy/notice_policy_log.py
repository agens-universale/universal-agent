from __future__ import annotations

import logging
from typing import Any

_logger = logging.getLogger(__name__)


def _resolve_logger(logger: Any | None) -> logging.Logger:
    if logger is not None and hasattr(logger, "info"):
        return logger
    return _logger


def log_notice_policy_step(
    logger: Any | None,
    *,
    tool_counters: dict[str, int],
    tool_thresholds: dict[str, int],
    tool_resets: dict[str, str],
    tool_emits: list[str],
    long_check_cnt: int | None,
    long_interval: int | None,
    long_reset: bool,
    long_emit: bool,
) -> None:
    """Debug-снимок счётчиков notice policy в текстовый agent log (не в thread meta)."""
    log = _resolve_logger(logger)
    parts: list[str] = []

    if tool_thresholds:
        tool_parts = []
        for tool_name in sorted(tool_thresholds):
            threshold = tool_thresholds[tool_name]
            value = tool_counters.get(tool_name, 0)
            tool_parts.append(f"{tool_name}={value}/{threshold}")
        parts.append(f"tool_usage_notice_cnt: {', '.join(tool_parts)}")

    if long_interval is not None and long_check_cnt is not None:
        parts.append(f"project_memory_check_cnt={long_check_cnt}/{long_interval}")

    if not parts:
        return

    suffix_parts: list[str] = []
    for tool_name in tool_emits:
        suffix_parts.append(f"emit tool_usage_notice[{tool_name}]")
    for tool_name, reason in sorted(tool_resets.items()):
        suffix_parts.append(f"tool_usage_notice[{tool_name}] {reason}")
    if long_emit:
        suffix_parts.append("emit project_memory_hint")
    if long_reset:
        suffix_parts.append("reset project_memory_check_cnt")

    line = "[notice_policy] " + "; ".join(parts)
    if suffix_parts:
        line += " | " + "; ".join(suffix_parts)
    log.info(line)
