"""Полный текст явного вызова `Think` для раскрытия строки ленты.

Сводка ядра (`tool_args_summary`) режет объект до одной короткой строки;
раскрытие показывает аргументы целиком: непустые `thoughts`, `critics`,
`doubts`, `reflection` по порядку, абзацами без подписей."""

from __future__ import annotations

from typing import Any, Optional

from gc_core.constants.tool_display import think_parts
from gc_core.constants.tool_names import SESSION_THINK_TOOL_NAME

from gc_tui.services.call_arguments import call_arguments


def think_text(wire_name: Optional[str], arguments: Any) -> str:
    """Тело раскрытия вызова `session_think`; другой тул — пусто."""
    if wire_name != SESSION_THINK_TOOL_NAME:
        return ""
    return "\n\n".join(think_parts(call_arguments(arguments)))
