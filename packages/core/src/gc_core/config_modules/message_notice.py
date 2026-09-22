from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES = 16


class ToolUsageNoticeRule(BaseModel):
    """Правило «не вызывали tool N persist-шагов» → technical notice."""

    model_config = ConfigDict(extra="forbid")

    tool_name: str
    step_messages_count: int = DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES
    notice_message: str


def normalize_tool_usage_notice_rules(rules: list[ToolUsageNoticeRule]) -> list[ToolUsageNoticeRule]:
    normalized: list[ToolUsageNoticeRule] = []
    for rule in rules:
        step_count = rule.step_messages_count
        if step_count <= 0:
            _logger.warning(
                "tool_usage_notice[%s].step_messages_count=%s must be > 0; using default %s",
                rule.tool_name,
                step_count,
                DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES,
            )
            step_count = DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES
        tool_name = str(rule.tool_name or "").strip()
        if not tool_name:
            _logger.warning("tool_usage_notice rule skipped: empty tool_name")
            continue
        notice_message = str(rule.notice_message or "").strip()
        if not notice_message:
            _logger.warning(
                "tool_usage_notice[%s] skipped: empty notice_message",
                tool_name,
            )
            continue
        normalized.append(
            ToolUsageNoticeRule(
                tool_name=tool_name,
                step_messages_count=step_count,
                notice_message=notice_message,
            )
        )
    return normalized


def tool_usage_notice_rules_from_config_dict(
    config_dict: dict[str, Any] | None,
) -> list[ToolUsageNoticeRule]:
    """Активные правила notice из ``tool_usage_notice`` / ``tool_usage_notice_params``."""
    raw = (config_dict or {}).get("tool_usage_notice")
    rules: list[ToolUsageNoticeRule] = []
    if isinstance(raw, list):
        for item in raw:
            if isinstance(item, ToolUsageNoticeRule):
                rules.append(item)
            elif isinstance(item, dict):
                rules.append(ToolUsageNoticeRule.model_validate(item))
    params = (config_dict or {}).get("tool_usage_notice_params")
    if not rules and isinstance(params, list):
        for item in params:
            if isinstance(item, dict):
                rules.append(ToolUsageNoticeRule.model_validate(item))
    return normalize_tool_usage_notice_rules(rules)


def _stopped_tools(config_dict: dict[str, Any] | None) -> set[str]:
    stop = config_dict.get("stop_tools") if isinstance(config_dict, dict) else None
    if not isinstance(stop, list):
        return set()
    return {str(item).strip() for item in stop if isinstance(item, str) and item.strip()}


def active_tool_usage_notice_rules(
    config_dict: dict[str, Any] | None,
) -> list[ToolUsageNoticeRule]:
    stopped = _stopped_tools(config_dict)
    return [rule for rule in tool_usage_notice_rules_from_config_dict(config_dict) if rule.tool_name not in stopped]


def tool_usage_notice_policy_enabled(config_dict: dict[str, Any] | None) -> bool:
    return bool(active_tool_usage_notice_rules(config_dict))
