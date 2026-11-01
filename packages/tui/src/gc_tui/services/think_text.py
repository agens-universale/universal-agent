"""Полный текст явного вызова `Think` для раскрытия строки ленты.

Сводка ядра (`tool_args_summary`) режет объект до одной короткой строки;
раскрытие показывает аргументы целиком: непустые `thoughts`, `critics`,
`doubts`, `reflection` по порядку, абзацами без подписей."""

from __future__ import annotations

from typing import Any, Optional

from gc_core.constants.tool_names import SESSION_THINK_TOOL_NAME

from gc_tui.services.call_arguments import call_arguments

_FIELDS = ("thoughts", "critics", "doubts", "reflection")


def think_text(wire_name: Optional[str], arguments: Any) -> str:
    """Тело раскрытия вызова `session_think`; другой тул — пусто."""
    if wire_name != SESSION_THINK_TOOL_NAME:
        return ""
    data = call_arguments(arguments)
    parts = (data.get(key) for key in _FIELDS)
    return "\n\n".join(
        p.strip() for p in parts if isinstance(p, str) and p.strip()
    )
