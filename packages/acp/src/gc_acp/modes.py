"""Режим разрешений сессии ACP: опция ``mode`` и ``modes``.

Режим — выбор сессии ядра (``executor.set_permission_mode``), тот же путь,
что у TUI и CLI. Клиент с ``configOptions`` видит опцию ``mode``, старый
клиент — ``modes``; оба вида строятся здесь из одного значения.
"""

from __future__ import annotations

from typing import Any

from gc_core.security.permission_mode import (
    AVAILABLE_MODES,
    coerce_mode,
    mode_label,
    mode_with_source,
)
from gc_core.settings import settings_of
from gc_core.threads.facade import Threads

from gc_acp.session_index import thread_key

MODE_CONFIG_ID = "mode"


def mode_config_option(mode: str) -> dict:
    """Select-опция режима: все собранные режимы в порядке цикла."""
    return {
        "id": MODE_CONFIG_ID,
        "name": "Mode",
        "category": "mode",
        "type": "select",
        "currentValue": mode,
        "options": [
            {"value": item.value, "name": mode_label(item)}
            for item in AVAILABLE_MODES
        ],
    }


def session_modes(mode: str) -> dict:
    """Поле ``modes`` ответа сессии — для клиентов без ``configOptions``."""
    return {
        "currentModeId": mode,
        "availableModes": [
            {"id": item.value, "name": mode_label(item)}
            for item in AVAILABLE_MODES
        ],
    }


def mode_choice(value: Any) -> str | None:
    """Значение из протокола → режим. Только точный id из списка."""
    if not isinstance(value, str):
        return None
    mode = coerce_mode(value)
    if mode is None or mode.value != value:
        return None
    return mode.value


def mode_updates(mode: str, config_options: list[dict]) -> list[dict]:
    """Уведомления о смене режима — в обоих видах, чтобы они не разошлись."""
    return [
        {"sessionUpdate": "current_mode_update", "currentModeId": mode},
        {
            "sessionUpdate": "config_option_update",
            "configOptions": config_options,
        },
    ]


async def acurrent_mode(
    agent: Any, settings: Any, *, threads: Threads, session_id: str
) -> str:
    """Действующий режим сессии — правило ядра ``mode_with_source``.

    Выбор знает исполнитель (смена из IDE, прошлый прогон этой сессии). До
    первого прогона после ``session/load`` его там ещё нет: режим сессии
    лежит только в состоянии треда.
    """
    executor = getattr(agent, "executor", None)
    getter = getattr(executor, "permission_mode", None)
    choice = getter() if callable(getter) else None
    if choice is None:
        choice = await _asaved_mode(threads, session_id)
    return mode_with_source(choice, settings_of(settings))[0].value


async def _asaved_mode(threads: Threads, session_id: str) -> str | None:
    """Выбор режима из состояния треда. Незнакомое значение отсеет
    ``mode_with_source``; негодный id сессии и сбой чтения — как нет
    выбора."""
    try:
        record = await threads.get(thread_key(session_id))
    except (OSError, ValueError):
        return None
    return None if record is None else record.state.permission_mode
