"""Витрина модели ACP: одна select-опция и уровень сессии.

Ядро не хранит выбранную модель отдельно от уровня. Пикер показывает алиас,
а смена пишет уровень этой сессии: его читает ``level_provider`` следующего
хода. ``config.yml`` фасад не трогает.
"""

from __future__ import annotations

from typing import Any

from gc_core.config import cli_forces_legacy_model
from gc_core.model_router import ModelRouter

_PICKER_LEVELS = ("default", "light", "max")
_OPTION_ID = "model"


def session_level(loaded: Any, override: str | None) -> str:
    """Уровень хода: выбор сессии, иначе ``settings.level`` или ``default``."""
    if override:
        return override
    configured = loaded.settings.level
    return configured or "default"


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

    router = _router(loaded, level, breaker)
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
    current = _alias_row(router, level)
    if current is None or current[0] not in seen:
        return None
    return _select(current[0], rows)


def config_options_payload(loaded: Any, level: str) -> dict:
    """Поле ответа сессии. Пустой список не отдаём."""
    option = model_config_option(loaded, level)
    if option is None:
        return {}
    return {"configOptions": [option]}


def listed(option: dict | None, value: Any) -> bool:
    if option is None or not isinstance(value, str):
        return False
    return any(item.get("value") == value for item in option["options"])


def level_for_choice(loaded: Any, level: str, value: str) -> str | None:
    """Уровень, который записать в сессию.

    ``None`` — успех без записи: единственная легаси-строка или алиас уже
    стоит за активным уровнем.
    """
    if _single_model(loaded):
        return None
    router = _router(loaded, level, None)
    current = _alias_row(router, level)
    if current is not None and current[0] == value:
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


def _router(loaded: Any, level: str, breaker: Any) -> ModelRouter:
    return ModelRouter(
        loaded,
        level_provider=lambda: level,
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


def _select(current: str, rows: list[tuple[str, str | None]]) -> dict:
    options = []
    for value, description in rows:
        item: dict[str, str] = {"value": value, "name": value}
        if description:
            item["description"] = description
        options.append(item)
    return {
        "id": _OPTION_ID,
        "name": "Model",
        "category": "model",
        "type": "select",
        "currentValue": current,
        "options": options,
    }
