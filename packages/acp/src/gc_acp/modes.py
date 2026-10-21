"""Режим разрешений сессии ACP: опция ``mode`` и поле ``modes``.

Режим — выбор сессии ядра (``executor.set_permission_mode``), тот же путь,
что у TUI и CLI. ``configOptions`` (``category: "mode"``) стоит рядом с
моделью: так рисует клиент, который показывает каждую select-опцию.
Поле ``modes`` — тот же список для клиента, который режим берёт только
оттуда. ``current_mode_update`` не шлём: такой клиент читает в нём
``modeId`` и затирает текущий режим. ``session/set_mode`` принимается и
отвечает ``config_option_update``.
"""

from __future__ import annotations

from typing import Any

from gc_core.persistence.thread_meta_mapper import ThreadMetaMapper
from gc_core.security.permission_mode import (
    AVAILABLE_MODES,
    coerce_mode,
    mode_label,
    mode_with_source,
)
from gc_core.settings import settings_of
from gc_core.thread_store import load_thread_meta

from gc_acp.session_index import ACP_SYSTEM

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
    """Поле ``modes`` ответа сессии: текущий режим и весь список."""
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


def mode_updates(config_options: list[dict]) -> list[dict]:
    """Смена режима — полный список опций, тем же видом, что и модель."""
    return [
        {
            "sessionUpdate": "config_option_update",
            "configOptions": config_options,
        },
    ]


def current_mode(
    agent: Any, settings: Any, *, threads_dir: str, session_id: str
) -> str:
    """Действующий режим сессии — правило ядра ``mode_with_source``.

    Выбор знает исполнитель (смена из IDE, прошлый прогон этой сессии). До
    первого прогона после ``session/load`` его там ещё нет: режим сессии
    лежит только в мете треда.
    """
    executor = getattr(agent, "executor", None)
    getter = getattr(executor, "permission_mode", None)
    choice = getter() if callable(getter) else None
    if choice is None:
        choice = _saved_mode(threads_dir, session_id)
    return mode_with_source(choice, settings_of(settings))[0].value


def _saved_mode(threads_dir: str, session_id: str) -> str | None:
    """Выбор режима из меты треда — через владельца формата меты
    (``ThreadMetaMapper``), а не разбором ключей здесь. Незнакомое значение
    отсеет ``mode_with_source``; негодная мета (``StateSchemaError`` —
    ``ValueError``) — как нет выбора."""
    try:
        meta = load_thread_meta(threads_dir, ACP_SYSTEM, session_id)
        shared = ThreadMetaMapper.shared_from_meta(meta)
    except (OSError, ValueError):
        return None
    return None if shared is None else shared.permission_mode
