"""Объединение встроенных тулов по режимам для тестов-стражей.

Режимы: `chat_completions` / `responses`, со скиллами и субагентами.
`turn_propose_plan` лежит в `tools_data` всегда (режим plan лишь решает,
уйдёт ли он в запрос), `session_compact` и `session_read_user_intent_log`
есть только в `chat_completions`, субагенты — тоже и только при включённой
фиче: объединение этих сборок покрывает все встроенные имена.
"""

from __future__ import annotations

from typing import Any

from gc_testkit.settings import make_settings

from gc_core.tools import abuild_tools_data

API_MODES = ("chat_completions", "responses")


async def builtin_tools_union() -> dict[str, dict[str, Any]]:
    """Имя тула -> его `spec` в объединении сборок по режимам."""
    union: dict[str, dict[str, Any]] = {}
    for api_mode in API_MODES:
        for subagents in (False, True):
            settings = make_settings(
                {
                    "backend.api_mode": api_mode,
                    "agent.use_local_subagents": subagents,
                    "paths.skills_data_path": "",
                }
            )
            tools_data, _, _, _ = await abuild_tools_data(settings)
            for name, data in tools_data.items():
                union.setdefault(name, data["spec"])
    return union
