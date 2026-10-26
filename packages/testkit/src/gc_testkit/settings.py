"""Настройки и контексты для тестов — без плоского словаря в коде агента.

Тест описывает конфиг путями через точку (``agent.max_steps``) и/или
вложенными секциями (``{"agent": {"max_steps": 3}}``) — так же, как
``--param`` и ``config.yml``. Коду агента уходят настоящие
``AppConfig``/``RuntimeEnv`` — строго провалидированные, как из файла.
Прежних плоских имён (``max_steps``) нет: такой ключ — ``ValidationError``.
"""

from __future__ import annotations

import copy
from types import SimpleNamespace
from typing import Any, Mapping, Optional

from gc_core.config_modules.provenance import Provenance
from gc_core.config_modules.root import AppConfig
from gc_core.settings import LoadedSettings, RuntimeEnv, _nested

#: Ключи теста, которые не настройки, а рантайм → поле ``RuntimeEnv``.
_RUNTIME_KEYS = {
    "launch_root": "launch_root",
    "project_root": "project_root",
    "trace_id": "trace_id",
    "text_answer_mode": "text_answer_mode",
    "finish_gate_smart": "finish_gate_smart",
    "subagent_tools_allow": "subagent_tools_allow",
    "subagent_tools_stop": "subagent_tools_stop",
    "os_backend": "os_backend",
    "run_mode": "run_mode",
}


def _runtime_value(key: str, value: Any) -> Any:
    if key.startswith("subagent_tools_") and value is not None:
        return tuple(value)
    return value


def _model_entry_input(entry: Any) -> Any:
    """Запись реестра моделей из её плоской проекции для ``ModelRouter``.

    Проекция хранит вычисленный ``authorization_header`` (``Bearer``-нормализация
    ``token``, уже с префиксом не дублируется) и ``backend_overrides`` — только
    явно заданные ключи backend. Обратно: ``token`` и ``backend``.
    """
    if not isinstance(entry, dict) or not (
        {"authorization_header", "backend_overrides"} & set(entry)
    ):
        return entry
    entry = dict(entry)
    if "authorization_header" in entry:
        header = entry.pop("authorization_header")
        if header is not None:
            entry["token"] = header
    overrides = entry.pop("backend_overrides", None)
    if overrides:
        entry["backend"] = overrides
    return {key: value for key, value in entry.items() if value is not None}


def make_settings(
    config: Optional[Mapping[str, Any]] = None, **dotted: Any
) -> LoadedSettings:
    """``LoadedSettings`` из описания теста: пути через точку и/или секции.

    ``make_settings({"agent.max_steps": 3})``,
    ``make_settings({"agent": {"max_steps": 3}})``,
    ``make_settings(**{"agent.max_steps": 3})``. Рантайм-ключи
    (``launch_root``, ``trace_id``, ``os_backend``…) уходят в ``RuntimeEnv``;
    ``profile`` — и указатель, и действующий профиль. Неизвестный ключ (в том
    числе прежнее плоское имя) или негодное значение — ``ValidationError``.
    Секции накладываются раньше своих листьев.
    """
    runtime: dict[str, Any] = {}
    updates: dict[str, Any] = {}
    for key, value in {**dict(config or {}), **dotted}.items():
        if key == "profile":
            runtime["active_profile"] = updates["profile"] = value or ""
            continue
        if key in _RUNTIME_KEYS:
            runtime[_RUNTIME_KEYS[key]] = _runtime_value(key, value)
            continue
        value = copy.deepcopy(value)
        if key == "models" and isinstance(value, dict):
            value = {
                alias: _model_entry_input(entry)
                for alias, entry in value.items()
            }
        updates[key] = value
    updates = dict(sorted(updates.items(), key=lambda kv: kv[0].count(".")))
    # Без resolve_paths: пути теста либо уже абсолютны, либо пусты —
    # «умолчание при использовании». Резолв подменил бы пустой путь путём от
    # cwd и полез бы в home — в песочнице workflow Temporal это запрещено.
    settings = AppConfig.model_validate(_nested(updates) if updates else {})
    return LoadedSettings(
        settings=settings,
        runtime=RuntimeEnv(**runtime),
        provenance=Provenance().with_layer("base", updates),
    )


def fake_ctx(
    config: Optional[Mapping[str, Any]] = None,
    *,
    loaded: Optional[LoadedSettings] = None,
    **attrs: Any,
) -> SimpleNamespace:
    """Минимальный контекст рана для тестов: ``settings``, ``runtime`` и любые
    дополнительные атрибуты (``logger=…``, ``os_backend=…``)."""
    loaded = loaded if loaded is not None else make_settings(config)
    attrs.setdefault("settings", loaded.settings)
    attrs.setdefault("runtime", loaded.runtime)
    return SimpleNamespace(**attrs)


def update_ctx(
    ctx: Any, config: Optional[Mapping[str, Any]] = None, **dotted: Any
) -> None:
    """Правка настроек готового тестового контекста — вместо ``ctx.config_dict[k] = v``.

    Ключи — как у :func:`make_settings` (пути через точку), рантайм-ключи
    (``launch_root``, ``run_mode``…) уходят в ``RuntimeEnv``. Значение-словарь
    по пути секции заменяет её значение. Негодное значение — ``ValidationError``.
    """
    runtime: dict[str, Any] = {}
    updates: dict[str, Any] = {}
    for key, value in {**dict(config or {}), **dotted}.items():
        if key in _RUNTIME_KEYS:
            runtime[_RUNTIME_KEYS[key]] = value
        else:
            updates[key] = value
    loaded = LoadedSettings(settings=ctx.settings, runtime=ctx.runtime)
    if updates:
        loaded = loaded.with_overrides(updates, layer="test")
    if runtime:
        loaded = loaded.with_runtime(**runtime)
    ctx.settings = loaded.settings
    ctx.runtime = loaded.runtime


__all__ = ["fake_ctx", "make_settings", "update_ctx"]
