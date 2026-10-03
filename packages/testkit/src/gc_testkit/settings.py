"""Настройки и контексты для тестов — без плоского словаря в коде агента.

Тест описывает конфиг как удобно: прежними плоскими именами (``max_steps``,
``write_guard``) и/или путями (``agent.max_steps``). Коду агента уходят
настоящие ``AppConfig``/``RuntimeEnv`` — строго провалидированные, как из файла.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, Mapping, Optional

from gc_core.config_modules.provenance import Provenance
from gc_core.config_modules.root import AppConfig
from gc_core.settings import LoadedSettings, RuntimeEnv, _nested

#: Плоские ключи рантайма → поле ``RuntimeEnv``.
_RUNTIME_FLAT = {
    "launch_root": "launch_root",
    "project_root": "project_root",
    "trace_id": "trace_id",
    "text_answer_mode": "text_answer_mode",
    "finish_gate_smart": "finish_gate_smart",
    "subagent_tools_allow": "subagent_tools_allow",
    "subagent_tools_stop": "subagent_tools_stop",
    "os_backend": "os_backend",
}
#: Вычисляемые плоские ключи: в настройки не переносятся.
_COMPUTED_FLAT = {
    "compat_notices",
    "voice_overridden_by_managed",
    "deprecated_keys_used",
    "cli_explicit_keys",
}


def _flat_key_is_set(flat: Mapping[str, Any], key: str) -> bool:
    """Задан ли ключ явно — там, где плоский вид сам это помнит.

    Плоский вид кладёт ``stream`` и ``tui_finish_gate_mode`` всегда; явность
    хранят ``deprecated_keys_used`` и ``tui_finish_gate_mode_explicit``. Без
    этих признаков (словарь собран руками) ключ считается заданным.
    """
    if key == "stream" and "deprecated_keys_used" in flat:
        return "backend.stream" in (flat.get("deprecated_keys_used") or ())
    if (
        key == "tui_finish_gate_mode"
        and "tui_finish_gate_mode_explicit" in flat
    ):
        return bool(flat.get("tui_finish_gate_mode_explicit"))
    return True


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


def settings_from_flat(
    flat: Mapping[str, Any],
    *,
    profile_meta: Any = None,
    config_dir: Optional[str] = None,
    layer: str = "base",
) -> LoadedSettings:
    """Словарь теста (прежние плоские имена ``config_dict``) → ``LoadedSettings``.

    Только для тестов: код агента словарей не принимает. Ключи переводятся
    по ``FLAT_TO_PATH`` (путь с точкой — как есть) и проходят валидацию модели;
    секции накладываются раньше своих листьев. Вычисляемые признаки
    (``*_explicit``, ``compat_notices``) пропускаются — их даёт происхождение.
    """
    import copy

    from gc_core.config_modules.flat_keys import FLAT_TO_PATH, setting_path

    runtime: dict[str, Any] = {}
    updates: dict[str, Any] = {}
    for key, value in flat.items():
        if key == "profile":
            # Плоский `profile` — действующий профиль; он же указатель.
            runtime["active_profile"] = updates["profile"] = value or ""
            continue
        if key in _RUNTIME_FLAT:
            if key.startswith("subagent_tools_") and value is not None:
                value = tuple(value)
            runtime[_RUNTIME_FLAT[key]] = value
            continue
        if key.startswith("_"):
            continue  # служебный ключ (кэш), не настройка
        if key in _COMPUTED_FLAT or key.endswith(
            ("_explicit", "_explicit_false")
        ):
            continue
        if not _flat_key_is_set(flat, key):
            continue
        path = setting_path(key)
        value = copy.deepcopy(value)
        if key == "models" and isinstance(value, dict):
            value = {
                alias: _model_entry_input(entry)
                for alias, entry in value.items()
            }
        if isinstance(value, dict) and path != key:
            # В секции плоского вида бывают плоские имена соседей
            # (`logging_params.logging_format` рядом с `format`) — переводим их.
            value = {
                (
                    FLAT_TO_PATH[sub][len(path) + 1 :]
                    if FLAT_TO_PATH.get(sub, "").startswith(path + ".")
                    else sub
                ): item
                for sub, item in value.items()
            }
        updates[path] = value
    updates = dict(sorted(updates.items(), key=lambda kv: kv[0].count(".")))
    # Без resolve_paths: пути плоского вида либо уже абсолютны (вид собран из
    # загруженных настроек), либо пусты — «умолчание при использовании», как
    # их и читал прежний код. Резолв подменил бы пустой путь путём от cwd и
    # полез бы в home — в песочнице workflow Temporal это запрещено.
    settings = AppConfig.model_validate(_nested(updates) if updates else {})
    return LoadedSettings(
        settings=settings,
        runtime=RuntimeEnv(config_dir=config_dir, **runtime),
        provenance=Provenance().with_layer(layer, updates),
        profile_meta=profile_meta,
    )


def make_settings(
    config: Optional[Mapping[str, Any]] = None, **dotted: Any
) -> LoadedSettings:
    """``LoadedSettings`` из словаря теста: плоские имена и пути через точку.

    ``make_settings({"max_steps": 3})``, ``make_settings(**{"agent.max_steps": 3})``.
    Рантайм-ключи (``launch_root``, ``trace_id``, ``profile``…) уходят в
    ``RuntimeEnv``. Неизвестный ключ или негодное значение — ``ValidationError``.
    """
    return settings_from_flat({**dict(config or {}), **dotted})


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


__all__ = ["fake_ctx", "make_settings", "settings_from_flat"]
