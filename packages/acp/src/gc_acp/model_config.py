"""Витрина модели ACP: одна select-опция и выбор сессии.

Пикер показывает все модели реестра, а смена пишет выбор этой сессии:
``choice_provider`` отдаёт его со следующего запроса к модели. Значение
строки и есть выбор (алиас или ``auto``). ``config.yml`` фасад не трогает.

Выбор ``auto`` — строка пикера, только когда ядро считает автовыбор
доступным этому конфигу (``auto_routing.levels``). Витрина не гадает модель
режима: пикер и трейс называют сам режим, а при недоступном ``auto``
показывают модель, которой ход пойдёт на деле (``effective_target``).
"""

from __future__ import annotations

from typing import Any

from gc_core.auto_routing.levels import (
    AUTO,
    auto_available,
    effective_target,
)
from gc_core.config import cli_forces_legacy_model
from gc_core.config_modules.model_registry import (
    configured_choice,
    effective_choice,
)
from gc_core.model_router import ModelRouter

_PICKER_LEVELS = ("light", "default", "max")
MODEL_CONFIG_ID = "model"


def session_choice(loaded: Any, override: str | None) -> str:
    """Выбор хода: выбор сессии, иначе ``settings.model`` или ``default``."""
    settings = loaded.settings
    return effective_choice(override or configured_choice(settings), settings)


def active_model_line(loaded: Any, choice: str) -> str | None:
    """Строка трейса: модель, которой пойдёт ход.

    ``None`` — резолв не дал имени. Открытый предохранитель строку не прячет:
    это витрина, как футер, а не запрос к транспорту. В ``auto`` строка
    называет режим: модель выбирается на каждый запрос.
    """
    available = _auto_available(loaded)
    if choice == AUTO and available:
        return f"Модель: {AUTO}"
    row = _resolved(_router(loaded, None), _shown(choice, available))
    if row is None:
        return None
    return _model_line(row.alias, row.model)


def model_config_option(
    loaded: Any, choice: str, *, breaker: Any = None
) -> dict | None:
    """Одна опция ``model`` или ``None``, если список пуст.

    Строки: модели уровней (каждая один раз, по младшему уровню), остальные
    модели реестра в его порядке, затем ``auto``, если он доступен.
    ``breaker`` — только чтобы открытый предохранитель не прятал строку.
    Фасад свой предохранитель в пикер не передаёт.
    """
    if _single_model(loaded):
        name = loaded.settings.agent.model
        if not name:
            return None
        return _select(name, [(name, None)])

    available = _auto_available(loaded)
    shown = _shown(choice, available)
    router = _router(loaded, breaker)
    rows = _model_rows(loaded, router)
    if not rows:
        return None
    if available:
        rows.append((AUTO, None))
        if choice == AUTO:
            return _select(AUTO, rows)
    current = _alias_row(router, shown)
    if current is None or current[0] not in {alias for alias, _ in rows}:
        return None
    return _select(current[0], rows)


def listed(option: dict | None, value: Any) -> bool:
    if option is None or not isinstance(value, str):
        return False
    return any(item.get("value") == value for item in option["options"])


def choice_for_value(loaded: Any, choice: str, value: str) -> str | None:
    """Выбор, который записать в сессию.

    ``None`` — успех без записи: единственная легаси-строка или модель уже
    стоит за активным выбором. Из ``auto`` любой алиас пишется выбором:
    это выход из режима.
    """
    if _single_model(loaded):
        return None
    available = _auto_available(loaded)
    auto_on = choice == AUTO and available
    if value == AUTO:
        return AUTO if available and not auto_on else None
    current = _alias_row(_router(loaded, None), _shown(choice, available))
    if not auto_on and current is not None and current[0] == value:
        return None
    return value


def _single_model(loaded: Any) -> bool:
    if not loaded.settings.models:
        return True
    return cli_forces_legacy_model(loaded)


def _auto_available(loaded: Any) -> bool:
    return auto_available(loaded, force_legacy=cli_forces_legacy_model(loaded))


def _shown(choice: str, available: bool) -> str:
    """Цель витрины: литерал ``auto`` в Router не попадает."""
    return effective_target(choice, None, available)


def _router(loaded: Any, breaker: Any) -> ModelRouter:
    return ModelRouter(
        loaded,
        choice_provider=lambda: "default",
        breaker=breaker,
        force_legacy=cli_forces_legacy_model(loaded),
    )


def _resolved(router: ModelRouter, target: str) -> Any:
    try:
        return router.resolve_main_at(target, check_breaker=False)
    except (ValueError, KeyError):
        return None


def _alias_row(
    router: ModelRouter, target: str
) -> tuple[str, str | None] | None:
    resolved = _resolved(router, target)
    if resolved is None or not resolved.alias:
        return None
    alias = resolved.alias
    model_id = resolved.model or alias
    description = model_id if model_id != alias else None
    return alias, description


def _model_rows(
    loaded: Any, router: ModelRouter
) -> list[tuple[str, str | None]]:
    """Модели уровней по младшему уровню, затем остальные в порядке реестра."""
    rows: list[tuple[str, str | None]] = []
    seen: set[str] = set()
    for target in (*_PICKER_LEVELS, *loaded.settings.models):
        found = _alias_row(router, target)
        if found is None or found[0] in seen:
            continue
        seen.add(found[0])
        rows.append(found)
    return rows


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
