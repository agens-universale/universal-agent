"""Витрина модели ACP: одна select-опция и уровень сессии.

Ядро не хранит выбранную модель отдельно от уровня. Пикер показывает алиас,
а смена пишет уровень этой сессии: ``choice_provider`` отдаёт его со
следующего запроса к модели. ``config.yml`` фасад не трогает.

Уровень ``auto`` — строка пикера, только когда ядро считает автовыбор
доступным этому конфигу (``auto_routing.levels``). Витрина не гадает модель
режима: пикер и трейс называют сам режим, а при недоступном ``auto``
показывают уровень, которым ход пойдёт на деле (``effective_target``).
"""

from __future__ import annotations

from typing import Any

from gc_core.auto_routing.levels import (
    AUTO,
    auto_available,
    effective_target,
)
from gc_core.config import cli_forces_legacy_model
from gc_core.model_router import ModelRouter

_PICKER_LEVELS = ("default", "light", "max")
MODEL_CONFIG_ID = "model"


def session_level(loaded: Any, override: str | None) -> str:
    """Уровень хода: выбор сессии, иначе ``settings.level`` или ``default``."""
    if override:
        return override
    configured = loaded.settings.level
    return configured or "default"


def active_model_line(loaded: Any, level: str) -> str | None:
    """Строка трейса: модель, которой пойдёт ход.

    ``None`` — резолв не дал имени. Открытый предохранитель строку не прячет:
    это витрина, как футер, а не запрос к транспорту. В ``auto`` строка
    называет режим: модель выбирается на каждый запрос.
    """
    available = _auto_available(loaded)
    if level == AUTO and available:
        return f"Модель: {AUTO}"
    try:
        resolved = _router(loaded, _shown(level, available), None).resolve(
            "main", check_breaker=False
        )
    except (ValueError, KeyError):
        return None
    return _model_line(resolved.alias, resolved.model)


def model_config_option(
    loaded: Any, level: str, *, breaker: Any = None
) -> dict | None:
    """Одна опция ``model`` или ``None``, если список пуст.

    ``breaker`` — только чтобы открытый предохранитель не прятал строку.
    Фасад свой предохранитель в пикер не передаёт.
    """
    if _single_model(loaded):
        name = loaded.settings.agent.model
        if not name:
            return None
        return _select(name, [(name, None)])

    available = _auto_available(loaded)
    shown = _shown(level, available)
    router = _router(loaded, shown, breaker)
    rows: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for item in _PICKER_LEVELS:
        found = _alias_row(router, item)
        if found is None or found[0] in seen:
            continue
        seen.add(found[0])
        rows.append(found)
    if not rows:
        return None
    if available:
        rows.append((AUTO, None))
        if level == AUTO:
            return _select(AUTO, rows)
    current = _alias_row(router, shown)
    if current is None or current[0] not in seen:
        return None
    return _select(current[0], rows)


def listed(option: dict | None, value: Any) -> bool:
    if option is None or not isinstance(value, str):
        return False
    return any(item.get("value") == value for item in option["options"])


def level_for_choice(loaded: Any, level: str, value: str) -> str | None:
    """Уровень, который записать в сессию.

    ``None`` — успех без записи: единственная легаси-строка или алиас уже
    стоит за активным уровнем. Из ``auto`` любой алиас пишет свой уровень:
    это выход из режима.
    """
    if _single_model(loaded):
        return None
    available = _auto_available(loaded)
    auto_on = level == AUTO and available
    if value == AUTO:
        return AUTO if available and not auto_on else None
    shown = _shown(level, available)
    router = _router(loaded, shown, None)
    current = _alias_row(router, shown)
    if not auto_on and current is not None and current[0] == value:
        return None
    for item in _PICKER_LEVELS:
        found = _alias_row(router, item)
        if found is not None and found[0] == value:
            return item
    return None


def _single_model(loaded: Any) -> bool:
    if not loaded.settings.models:
        return True
    return cli_forces_legacy_model(loaded)


def _auto_available(loaded: Any) -> bool:
    return auto_available(loaded, force_legacy=cli_forces_legacy_model(loaded))


def _shown(level: str, available: bool) -> str:
    """Уровень витрины: литерал ``auto`` в Router не попадает."""
    return effective_target(level, None, available)


def _router(loaded: Any, level: str, breaker: Any) -> ModelRouter:
    return ModelRouter(
        loaded,
        choice_provider=lambda: level,
        breaker=breaker,
        force_legacy=cli_forces_legacy_model(loaded),
    )


def _alias_row(
    router: ModelRouter, level: str
) -> tuple[str, str | None] | None:
    try:
        resolved = router.resolve_level(level, check_breaker=False)
    except (ValueError, KeyError):
        return None
    alias = resolved.alias
    if not alias:
        return None
    model_id = resolved.model or alias
    description = model_id if model_id != alias else None
    return alias, description


def _model_line(alias: str | None, model: str | None) -> str | None:
    name = (model or "").strip()
    label = (alias or "").strip()
    if label and name and label != name:
        return f"Модель: {label} ({name})"
    shown = name or label
    if shown == "":
        return None
    return f"Модель: {shown}"


def _select(current: str, rows: list[tuple[str, str | None]]) -> dict:
    options = []
    for value, description in rows:
        item: dict[str, str] = {"value": value, "name": value}
        if description:
            item["description"] = description
        options.append(item)
    return {
        "id": MODEL_CONFIG_ID,
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": current,
        "options": options,
    }
