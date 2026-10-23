"""Заголовок блока `Agent` из сырых аргументов вызова субагента (решение
владельца 2026-10-23, спека P4 §7): короткая роль — объект строки, задача
— строка `└` под ней. Одна функция для живой ленты, возврата и превью:
её зовёт маппер трейса (до облегчения retention)."""

from __future__ import annotations

import json
from typing import Any, Optional

from gc_core.constants.tool_display import (
    DisplayCategory,
    subagent_role,
    tool_display,
)
from gc_core.constants.tool_names import SUBAGENT_SPECIAL_AGENT_NAME

from gc_tui.models import AgentHead


def _arguments(arguments: Any) -> dict:
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments) if arguments.strip() else {}
        except json.JSONDecodeError:
            return {}
    return arguments if isinstance(arguments, dict) else {}


def _text(data: dict, key: str) -> str:
    value = data.get(key)
    return value.strip() if isinstance(value, str) else ""


def agent_head(
    wire_name: Optional[str], arguments: Any
) -> Optional[AgentHead]:
    """Заголовок вызова субагента; не субагент — `None`. Роль — таблица
    ядра (`subagent_role`), у `subagent_special_agent` — его аргумент
    `role`."""
    if not wire_name:
        return None
    if tool_display(wire_name).category is not DisplayCategory.AGENT:
        return None
    data = _arguments(arguments)
    role = ""
    if wire_name == SUBAGENT_SPECIAL_AGENT_NAME:
        role = _text(data, "role")
    return AgentHead(role or subagent_role(wire_name), _text(data, "task"))
