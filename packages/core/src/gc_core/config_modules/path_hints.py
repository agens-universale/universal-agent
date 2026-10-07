"""Пути настроек: знает ли схема путь, и подсказка для незнакомого имени.

Плоских имён прежнего ``config_dict`` больше нет: ``--param``,
``agent_overrides`` сервера, ``config explain`` и профили бенча принимают
только путь схемы через точку (``agent.max_steps``). Для имени, которого
схема не знает, подсказка ищет листья и секции с тем же последним сегментом:
``max_steps`` → ``agent.max_steps``, ``write_guard`` → ``security.write_guard``.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Any, Iterable

from gc_core.config_modules.field_meta import _nested_model, leaf_meta


class UnknownSettingError(ValueError):
    """Имя настройки, которого схема не знает (с подсказкой пути)."""

    def __init__(self, name: str, *, prefix: str = "") -> None:
        self.name = name
        self.suggestions = tuple(suggest_paths(name))
        super().__init__(
            unknown_setting_message(name, self.suggestions, prefix=prefix)
        )


def _field(model: Any, name: str) -> Any:
    fields = getattr(model, "model_fields", None) or {}
    return fields.get(name) or next(
        (info for info in fields.values() if info.alias == name), None
    )


def is_known_path(dotted: str) -> bool:
    """Путь ведёт в схему ``AppConfig``: по полям (имя или alias), через ключи
    словарей моделей (``models.<alias>``) до листа. Глубже листа-словаря
    (``backend.chat_completion_kwargs.temperature``) и внутри секции с
    произвольными ключами путь не проверяется — это решает валидация."""
    from gc_core.config_modules.root import AppConfig

    current: Any = AppConfig
    entry_key = False
    for name in dotted.split("."):
        if not name:
            return False
        if entry_key:
            entry_key = False  # имя записи словаря моделей — любое
            continue
        info = _field(current, name)
        if info is None:
            config = getattr(current, "model_config", None) or {}
            return config.get("extra") == "allow"
        kind, nested = _nested_model(info.annotation)
        if kind == "leaf":
            return True
        entry_key = kind == "dict"
        current = nested
    return True


@lru_cache(maxsize=1)
def _paths_by_last_segment() -> dict[str, tuple[str, ...]]:
    """``{последний сегмент: пути листьев и секций с ним}``."""
    found: dict[str, set[str]] = {}
    for leaf in leaf_meta():
        parts = leaf.split(".")
        for depth in range(1, len(parts) + 1):
            if "*" in parts[:depth]:
                break
            found.setdefault(parts[depth - 1], set()).add(
                ".".join(parts[:depth])
            )
    return {name: tuple(sorted(paths)) for name, paths in found.items()}


#: Прежние имена, у которых нет совпадающего последнего сегмента в схеме.
_RENAMED = {"agent_url": "backend.completions_url"}


def suggest_paths(name: str) -> list[str]:
    """Пути схемы, чей последний сегмент совпадает с последним сегментом
    ``name``: ``max_steps`` → ``["agent.max_steps"]``; прежнее имя без такого
    совпадения — по таблице (``agent_url`` → ``backend.completions_url``)."""
    if name in _RENAMED:
        return [_RENAMED[name]]
    last = name.rsplit(".", 1)[-1]
    return [
        path for path in _paths_by_last_segment().get(last, ()) if path != name
    ]


def unknown_setting_message(
    name: str, suggestions: Iterable[str] = (), *, prefix: str = ""
) -> str:
    """``неизвестная настройка --max_steps (возможно, --agent.max_steps)``."""
    text = f"неизвестная настройка {prefix}{name}"
    options = [f"{prefix}{path}" for path in suggestions]
    if len(options) == 1:
        text += f" (возможно, {options[0]})"
    elif options:
        text += f" (возможно, одна из: {', '.join(options)})"
    return text


def check_setting_path(dotted: str, *, prefix: str = "") -> None:
    """``UnknownSettingError`` с подсказкой, если схема пути не знает."""
    if not is_known_path(dotted):
        raise UnknownSettingError(dotted, prefix=prefix)


__all__ = [
    "UnknownSettingError",
    "check_setting_path",
    "is_known_path",
    "suggest_paths",
    "unknown_setting_message",
]
