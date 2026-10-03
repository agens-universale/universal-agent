from __future__ import annotations

import logging
from typing import Any

from pydantic import BaseModel, ConfigDict

_logger = logging.getLogger(__name__)

DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES = 16


class ToolUsageNoticeRule(BaseModel):
    """Правило «не вызывали tool N persist-шагов» → technical notice."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    tool_name: str
    step_messages_count: int = DEFAULT_TOOL_USAGE_NOTICE_STEP_MESSAGES
    notice_message: str


def normalize_tool_usage_notice_rules(
    rules: list[ToolUsageNoticeRule],
) -> list[ToolUsageNoticeRule]:
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
    settings: Any,
) -> list[ToolUsageNoticeRule]:
    """Правила notice из ``tool_usage_notice`` настроек (нормализованные).

    ``settings`` — всё, что принимает ``settings_of``.
    """
    # Ленивый импорт: settings → root → этот модуль.
    from gc_core.settings import settings_of

    return normalize_tool_usage_notice_rules(
        list(settings_of(settings).tool_usage_notice)
    )


def _stopped_tools(settings: Any) -> set[str]:
    from gc_core.settings import settings_of

    return {
        item.strip()
        for item in settings_of(settings).agent.stop_tools
        if item.strip()
    }


def active_tool_usage_notice_rules(
    settings: Any,
) -> list[ToolUsageNoticeRule]:
    stopped = _stopped_tools(settings)
    return [
        rule
        for rule in tool_usage_notice_rules_from_config_dict(settings)
        if rule.tool_name not in stopped
    ]


def tool_usage_notice_policy_enabled(
    settings: Any,
) -> bool:
    return bool(active_tool_usage_notice_rules(settings))
