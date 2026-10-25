"""Выбор модели в TUI: строки пикера `/model`, подсказки, значение для
палитры и проверка набранного имени (спека model-knob §6.2, §6.3, §3.2,
§3.3, §4.4).

Один владелец правила «что показать и что можно выбрать»: команда `/model`,
палитра, `/status` и строка «Режимов работы» спрашивают отсюда."""

from __future__ import annotations

from typing import Any, Optional

from gc_core.config_modules.model_registry import LEVELS, choice_in_registry
from gc_core.settings import settings_of
from rich.text import Text

from gc_tui.services import model_sign
from gc_tui.toggles import auto_unavailable_reason_cfg, shown_choice
from gc_tui.widgets.arg_picker import PickerOption

AUTO = "auto"
AUTO_DESC = "модель выбирается по ходу работы"
ACTIVE_STATUS = "● активна"
UNAVAILABLE_HINT = "недоступна в этой сессии"
LEGACY_HINT = "легаси: выбор модели появится с реестром моделей"
LEGACY_FORCE_HINT = "--model: выбор модели отключён на этот запуск"
UNKNOWN_REFUSAL = "✗ нет такой модели: {arg}"
_SEPARATOR = PickerOption("", "", separator=True)


def _router(app: Any, loaded: Any) -> Any:
    """Роутер витрины для настроек `loaded` (действующих или цели записи):
    предохранитель окна и легаси-форс этих настроек."""
    from gc_core.config import cli_forces_legacy_model
    from gc_core.model_router import ModelRouter

    return ModelRouter(
        loaded,
        choice_provider=lambda: "default",
        breaker=app.display_router().breaker,
        force_legacy=cli_forces_legacy_model(loaded),
    )


def _legacy(router: Any) -> bool:
    return router.force_legacy or router.registry_empty()


def _slots(loaded: Any) -> dict:
    return settings_of(loaded).slots.model_dump()


def _alias_of(router: Any, choice: str) -> str:
    """Модель выбора: слово уровня — модель своего слота, иначе как есть."""
    if choice not in LEVELS:
        return choice
    try:
        resolved = router.resolve_level(choice, check_breaker=False)
    except ValueError:
        return choice
    return resolved.alias or choice


def _level_models(slots: dict, models: Any) -> list[str]:
    """Модели уровней, каждая один раз, по её младшему уровню."""
    out: list[str] = []
    for level in LEVELS:
        alias = slots.get(level)
        if alias in models and alias not in out:
            out.append(alias)
    return out


def _legacy_options(router: Any) -> tuple[PickerOption, ...]:
    """Легаси: выбор не действует — auto и плоская модель видны, но не
    выбираются; плоская модель — та, что работает."""
    model = router.resolve("main", check_breaker=False).model
    return (
        PickerOption(
            AUTO, AUTO_DESC, lead=model_sign.dots(AUTO, {}), enabled=False
        ),
        _SEPARATOR,
        PickerOption(
            model,
            "",
            ACTIVE_STATUS,
            model_sign.dots(model, {}),
            enabled=False,
        ),
    )


def model_picker_options(
    app: Any, settings: Any = None
) -> tuple[PickerOption, ...]:
    """Строки пикера: `auto`, черта, модели уровней, черта, остальные
    модели реестра. «● активна» — на выборе `settings` (по умолчанию —
    действующие настройки окна; «Настройки» передают настройки цели
    записи). Пустой блок — без своей черты."""
    loaded = settings if settings is not None else app.effective_settings()
    router = _router(app, loaded)
    if _legacy(router):
        return _legacy_options(router)
    slots = _slots(loaded)
    models = settings_of(loaded).models
    levels = _level_models(slots, models)
    rest = [alias for alias in models if alias not in levels]
    active = _alias_of(router, shown_choice(loaded))

    def row(value: str, desc: str = "", enabled: bool = True):
        status = ACTIVE_STATUS if value == active else ""
        lead = model_sign.dots(value, slots)
        return PickerOption(value, desc, status, lead, enabled)

    available = auto_unavailable_reason_cfg(loaded) is None
    options = [row(AUTO, AUTO_DESC, available)]
    for block in (levels, rest):
        if block:
            options += [_SEPARATOR, *(row(alias) for alias in block)]
    return tuple(options)


def _auto_levels(loaded: Any) -> list[tuple[str, str]]:
    """Пары уровней, которыми реально ходит автовыбор: обе — при лестнице
    пар (`safe_level: auto` уцелел после сборки), иначе одна."""
    from dataclasses import replace

    from gc_core.auto_routing.levels import resolve_routing_params
    from gc_core.auto_routing.policy import (
        PAIR_LOWER,
        PAIR_UPPER,
        SAFE_LEVEL_AUTO,
        pair_levels,
    )

    params = resolve_routing_params(loaded)
    if params.safe_level != SAFE_LEVEL_AUTO:
        return [pair_levels(params)]
    return [
        pair_levels(replace(params, safe_level=level))
        for level in (PAIR_LOWER, PAIR_UPPER)
    ]


def _auto_hint(router: Any, loaded: Any) -> str:
    """Пары автовыбора; недоступный автовыбор — его причина."""
    reason = auto_unavailable_reason_cfg(loaded)
    if reason is not None:
        from gc_core.startup_notices import (
            auto_routing_unavailable_reason_text,
        )

        return auto_routing_unavailable_reason_text(reason)
    pairs = [
        f"{_alias_of(router, weak)} и {_alias_of(router, strong)}"
        for weak, strong in _auto_levels(loaded)
    ]
    head = "пары автовыбора" if len(pairs) > 1 else "пара автовыбора"
    return f"{head}: " + ", ".join(pairs)


def model_hint(app: Any, value: str) -> str:
    """Подсказка про модель `value` — без её имени: уровни модели, «без
    уровня», «недоступна в этой сессии»; у `auto` — пары или причина."""
    loaded = app.effective_settings()
    router = _router(app, loaded)
    if value == AUTO:
        return _auto_hint(router, loaded)
    if _legacy(router):
        return LEGACY_FORCE_HINT if router.force_legacy else LEGACY_HINT
    alias = _alias_of(router, value)
    if router.breaker.is_open(alias):
        return UNAVAILABLE_HINT
    return model_sign.level_hint(alias, _slots(loaded))


def model_picker_hint(app: Any, value: Optional[str]) -> str:
    """Подвал пикера: «<модель> — <подсказка>»."""
    if not value:
        return ""
    return f"{value} — {model_hint(app, value)}"


def current_choice(app: Any) -> str:
    """Действующий выбор окна для витрин (`/status`)."""
    return shown_choice(app.effective_settings())


def model_value(app: Any) -> Text:
    """Значение `/model` в палитре: знак + имя, у auto — «● auto»;
    в легаси — плоская модель."""
    loaded = app.effective_settings()
    choice = shown_choice(loaded)
    slots = _slots(loaded)
    if choice == AUTO:
        return model_sign.sign(AUTO, slots)
    router = _router(app, loaded)
    if _legacy(router):
        model = router.resolve("main", check_breaker=False).model
        return model_sign.sign(model, {})
    return model_sign.sign(_alias_of(router, choice), slots)


def model_choice_refusal(app: Any, value: str) -> Optional[str]:
    """Почему набранный выбор не принимается, или None — принимается:
    имя вне реестра, `auto` при недоступном автовыборе (с причиной)."""
    loaded = app.effective_settings()
    if not choice_in_registry(value, settings_of(loaded)):
        return UNKNOWN_REFUSAL.format(arg=value)
    if value != AUTO:
        return None
    reason = auto_unavailable_reason_cfg(loaded)
    if reason is None:
        return None
    from gc_core.startup_notices import auto_routing_unavailable_reason_text

    return f"/model auto — {auto_routing_unavailable_reason_text(reason)}"
