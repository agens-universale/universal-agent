"""Лишние ключи в YAML: отбросить с предупреждением, а не ронять загрузку.

Модели конфига строгие (``extra="forbid"``) — опечатка в коде или в ``--param``
падает сразу. А файл пишет человек: устаревший ключ, опечатка или поле из
более новой версии не должны лишать его агента. Перед валидацией документ
проходит по схеме; ключ, которого схема не знает, удаляется и попадает в
уведомление — с координатой и, если найдётся, подсказкой «возможно, …».

Принятые имена поля повторяют правило pydantic: при ``alias`` имя поля
принимается, только если модель разрешает заполнение по имени.
"""

from __future__ import annotations

import difflib
from dataclasses import dataclass
from typing import Any, Iterable, Mapping, get_args, get_origin

from pydantic import AliasChoices, AliasPath, BaseModel


@dataclass(frozen=True)
class UnknownKey:
    """Отброшенный ключ: путь сегментами и ближайшее известное имя."""

    parts: tuple[str, ...]
    suggestion: str | None = None

    @property
    def dotted(self) -> str:
        return ".".join(self.parts)


def _alias_names(alias: Any) -> Iterable[str]:
    if isinstance(alias, str):
        yield alias
    elif isinstance(alias, AliasChoices):
        for choice in alias.choices:
            yield from _alias_names(choice)
    elif isinstance(alias, AliasPath) and alias.path:
        first = alias.path[0]
        if isinstance(first, str):
            yield first


def accepted_keys(model: type[BaseModel]) -> dict[str, str]:
    """``{ключ документа: имя поля}`` — все написания, которые примет модель."""
    config = model.model_config
    by_name = bool(
        config.get("populate_by_name") or config.get("validate_by_name")
    )
    keys: dict[str, str] = {}
    for name, info in model.model_fields.items():
        aliases = list(_alias_names(info.validation_alias)) or list(
            _alias_names(info.alias)
        )
        for alias in aliases:
            keys[alias] = name
        if not aliases or by_name:
            keys[name] = name
    return keys


def _shape(annotation: Any) -> tuple[str, type[BaseModel] | None]:
    """``model`` / ``dict`` (словарь моделей) / ``list`` (список моделей) / ``leaf``."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return "model", annotation
    origin = get_origin(annotation)
    args = get_args(annotation)
    if origin is dict and len(args) == 2:
        kind, nested = _shape(args[1])
        return ("dict", nested) if kind == "model" else ("leaf", None)
    if origin in (list, tuple) and args:
        kind, nested = _shape(args[0])
        return ("list", nested) if kind == "model" else ("leaf", None)
    for arg in args:  # Optional / Union
        kind, nested = _shape(arg)
        if kind != "leaf":
            return kind, nested
    return "leaf", None


def drop_unknown_keys(
    document: Any, model: type[BaseModel]
) -> tuple[Any, list[UnknownKey]]:
    """Копия документа без ключей, которых не знает схема, и их список."""
    dropped: list[UnknownKey] = []
    return _walk(document, model, (), dropped), dropped


def _walk(
    value: Any,
    model: type[BaseModel],
    parts: tuple[str, ...],
    dropped: list[UnknownKey],
) -> Any:
    if not isinstance(value, Mapping):
        return value
    if model.model_config.get("extra") == "allow":
        return dict(value)
    accepted = accepted_keys(model)
    out: dict[Any, Any] = {}
    for key, item in value.items():
        field_name = accepted.get(key) if isinstance(key, str) else None
        if field_name is None:
            close = difflib.get_close_matches(
                str(key), list(accepted), n=1, cutoff=0.6
            )
            dropped.append(
                UnknownKey((*parts, str(key)), close[0] if close else None)
            )
            continue
        kind, nested = _shape(model.model_fields[field_name].annotation)
        path = (*parts, str(key))
        if kind == "model":
            item = _walk(item, nested, path, dropped)
        elif kind == "dict" and isinstance(item, Mapping):
            item = {
                entry: _walk(sub, nested, (*path, str(entry)), dropped)
                for entry, sub in item.items()
            }
        elif kind == "list" and isinstance(item, list):
            item = [
                _walk(sub, nested, (*path, str(index)), dropped)
                for index, sub in enumerate(item)
            ]
        out[key] = item
    return out


def unknown_key_notice(unknown: UnknownKey, source: str | None = None) -> str:
    """Текст уведомления об отброшенном ключе.

    Устаревшие ключи сюда не доходят: их раньше снимает ``compat`` (REMOVED)
    со своим пояснением — например, поля backend, неприменимые на модели."""
    where = f"{source}: " if source else ""
    hint = f" — возможно, «{unknown.suggestion}»" if unknown.suggestion else ""
    if unknown.dotted.startswith("security."):
        # Опечатка в защите молча ослабляет её (список запретов, режим
        # модерации) — уведомление должно бросаться в глаза.
        return (
            f"{where}ВНИМАНИЕ: настройка защиты не применена — неизвестный "
            f"ключ «{unknown.dotted}» проигнорирован{hint}"
        )
    return f"{where}неизвестный ключ «{unknown.dotted}» проигнорирован{hint}"


__all__ = [
    "UnknownKey",
    "accepted_keys",
    "drop_unknown_keys",
    "unknown_key_notice",
]
