"""Настройки агента: неизменяемая модель, рантайм-окружение и их происхождение.

``AppConfig`` — то, что задал пользователь (файлы, слои, env, CLI, сессия);
модели неизменяемы, правка — ``override_settings`` / ``LoadedSettings.with_overrides``
даёт новый провалидированный объект. ``RuntimeEnv`` — то, что вычисляет запуск
(каталог старта, активный профиль, notices), а не пользователь.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional, get_args, get_origin

from pydantic import BaseModel

from gc_core.config_modules.merge import deep_merge
from gc_core.config_modules.provenance import Provenance
from gc_core.config_modules.root import AppConfig, resolve_paths


@dataclass(frozen=True)
class RuntimeEnv:
    """Что вычисляет запуск, а не задаёт пользователь."""

    config_path: Optional[str] = None
    config_dir: Optional[str] = None
    #: Действующий профиль (указатель файлов либо ``--profile``/сессия).
    active_profile: str = ""
    #: Сообщения загрузки: compat, секции моделей, профили.
    notices: tuple[str, ...] = ()
    #: Managed-слой перекрыл пользовательский voice (имя поля для notices).
    voice_overridden_by_managed: Optional[str] = None
    #: Снимок cwd на старте: периметр записи, project data, Diff-TUI.
    launch_root: Optional[str] = None
    project_root: Optional[str] = None
    trace_id: Optional[str] = None
    is_tui: bool = False
    #: Сигналы поверхности (только TUI): адаптивный bounce текстового ответа
    #: (``tui.text_answer_mode``) и умный режим судьи финиша (``/gate smart``).
    #: CLI/server их не ставят — строгий контракт.
    text_answer_mode: Optional[str] = None
    finish_gate_smart: bool = False
    #: Область инструментов субагента из его файла: белый список (None — без
    #: ограничения) и дополнительные запреты поверх глобального ``stop_tools``.
    subagent_tools_allow: Optional[tuple[str, ...]] = None
    subagent_tools_stop: tuple[str, ...] = ()
    #: Вид OS-бэкенда, выбранный встраивающим (``local``/``harbor``); None — local.
    os_backend: Optional[str] = None

    def with_values(self, **values: Any) -> RuntimeEnv:
        return replace(self, **values)


def _alias_path(model: type[BaseModel], path: list[str]) -> list[str]:
    """Имена полей пути → ключи документа (alias, если у поля он есть)."""
    keys: list[str] = []
    current: Any = model
    for name in path:
        if isinstance(current, _DictOf):
            keys.append(name)  # ключ словаря: сервер MCP, алиас модели
            current = current.value_type
            continue
        fields = getattr(current, "model_fields", None)
        if not fields or name not in fields:
            keys.append(name)
            current = None
            continue
        info = fields[name]
        keys.append(info.alias or name)
        annotation = info.annotation
        nested = None
        for candidate in (annotation, *get_args(annotation)):
            if isinstance(candidate, type) and issubclass(
                candidate, BaseModel
            ):
                nested = candidate
                break
        if nested is None and get_origin(annotation) is dict:
            args = get_args(annotation)
            value_type = args[1] if len(args) == 2 else None
            if isinstance(value_type, type) and issubclass(
                value_type, BaseModel
            ):
                # Следующий сегмент — ключ словаря, затем поля его модели.
                current = _DictOf(value_type)
                continue
        current = nested
    return keys


class _DictOf:
    """Маркер «следующий сегмент — ключ словаря моделей ``value_type``»."""

    def __init__(self, value_type: type[BaseModel]):
        self.value_type = value_type


def _nested(updates: Mapping[str, Any]) -> dict[str, Any]:
    document: dict[str, Any] = {}
    for dotted, value in updates.items():
        keys = _alias_path(AppConfig, dotted.split("."))
        target = document
        for key in keys[:-1]:
            target = target.setdefault(key, {})
        target[keys[-1]] = value
    return document


def override_settings(
    settings: AppConfig,
    updates: Mapping[str, Any],
    *,
    config_dir: Optional[str] = None,
) -> AppConfig:
    """Новый ``AppConfig`` с точечными правками ``{"agent.max_steps": 5}``.

    Правки проходят ту же валидацию и нормализацию, что и файл: недопустимое
    значение — ``ValidationError``, относительный путь — от ``config_dir``.
    Явно заданные поля исходных настроек остаются явными.
    """
    if not updates:
        return settings
    base = settings.model_dump(by_alias=True, exclude_unset=True)
    merged = deep_merge(base, _nested(updates))
    rebuilt = AppConfig.model_validate(merged)
    return resolve_paths(rebuilt, config_dir or os.getcwd())


@dataclass(frozen=True)
class LoadedSettings:
    """Итог загрузки: настройки, рантайм и происхождение каждого значения."""

    settings: AppConfig
    runtime: RuntimeEnv = field(default_factory=RuntimeEnv)
    provenance: Provenance = field(default_factory=Provenance)
    profile_meta: Any = None

    def with_overrides(
        self, updates: Mapping[str, Any], *, layer: str = "cli"
    ) -> LoadedSettings:
        """Правки поверх загруженного; источник правок помечается ``layer``."""
        if not updates:
            return self
        return replace(
            self,
            settings=override_settings(
                self.settings, updates, config_dir=self.runtime.config_dir
            ),
            provenance=self.provenance.with_layer(layer, updates),
        )

    def with_runtime(self, **values: Any) -> LoadedSettings:
        return replace(self, runtime=self.runtime.with_values(**values))


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
    """Переходно: плоский словарь прежнего ``config_dict`` → ``LoadedSettings``.

    Для тестов и миграции, пока они собирают конфиг словарём. Ключи переводятся
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
            continue  # кэш производных значений (derived_cache), не настройка
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


def _loaded_of(source: Any) -> LoadedSettings:
    """LoadedSettings из контекста, настроек или (переходно) плоского словаря."""
    if isinstance(source, LoadedSettings):
        return source
    if isinstance(source, AppConfig):
        return LoadedSettings(settings=source)
    settings = getattr(source, "settings", None)
    if settings is not None and not isinstance(source, Mapping):
        if isinstance(settings, LoadedSettings):
            return settings
        runtime = getattr(source, "runtime", None) or RuntimeEnv()
        return LoadedSettings(settings=settings, runtime=runtime)
    flat = (
        source
        if isinstance(source, Mapping)
        else getattr(source, "config_dict", None)
    )
    if flat is None:
        return LoadedSettings(settings=AppConfig())
    # Переходно (тесты с плоским словарём): строгая конвертация, кэш при
    # неизменяемом виде.
    from gc_core.config_modules.frozen import FrozenConfigDict, derived_cache

    if isinstance(flat, FrozenConfigDict):
        cache = derived_cache(flat)
        if "_settings" not in cache:
            cache["_settings"] = settings_from_flat(flat)
        return cache["_settings"]
    return settings_from_flat(flat)


def settings_of(source: Any) -> AppConfig:
    """Настройки (``AppConfig``) из ``RunContext``, ``LoadedSettings``, ``AppConfig``.

    Единая точка чтения для потребителей. Переходно принимает и плоский
    ``config_dict`` (или объект с ним) — конвертирует строго: неизвестный ключ —
    ``ValidationError``.
    """
    return _loaded_of(source).settings


def runtime_of(source: Any) -> RuntimeEnv:
    """``RuntimeEnv`` из того же набора источников, что ``settings_of``."""
    return _loaded_of(source).runtime


def is_explicit(model: BaseModel, dotted: str) -> bool:
    """Задано ли поле явно (файлом, слоем, CLI, сессией), а не умолчанием.

    ``is_explicit(settings, "tui.finish_gate_mode")`` — по ``model_fields_set``
    каждого уровня пути.
    """
    current: Any = model
    for name in dotted.split("."):
        if (
            not isinstance(current, BaseModel)
            or name not in current.model_fields_set
        ):
            return False
        current = getattr(current, name)
    return True


def default_settings(**updates: Any) -> AppConfig:
    """Настройки по умолчанию (для тестов и встраивания): ``default_settings(**{"agent.max_steps": 3})``."""
    base = resolve_paths(AppConfig(), os.getcwd())
    return override_settings(base, updates) if updates else base


__all__ = [
    "AppConfig",
    "LoadedSettings",
    "RuntimeEnv",
    "default_settings",
    "is_explicit",
    "override_settings",
    "runtime_of",
    "settings_from_flat",
    "settings_of",
]
