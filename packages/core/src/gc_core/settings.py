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
}
#: Вычисляемые плоские ключи: в настройки не переносятся.
_COMPUTED_FLAT = {
    "compat_notices",
    "voice_overridden_by_managed",
    "deprecated_keys_used",
    "cli_explicit_keys",
}


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
            runtime[_RUNTIME_FLAT[key]] = value
            continue
        if key in _COMPUTED_FLAT or key.endswith(
            ("_explicit", "_explicit_false")
        ):
            continue
        path = setting_path(key)
        value = copy.deepcopy(value)
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
    root = config_dir or os.getcwd()
    base = resolve_paths(AppConfig(), root)
    return LoadedSettings(
        settings=override_settings(base, updates, config_dir=root),
        runtime=RuntimeEnv(config_dir=root, **runtime),
        provenance=Provenance().with_layer(layer, updates),
        profile_meta=profile_meta,
    )


def default_settings(**updates: Any) -> AppConfig:
    """Настройки по умолчанию (для тестов и встраивания): ``default_settings(**{"agent.max_steps": 3})``."""
    base = resolve_paths(AppConfig(), os.getcwd())
    return override_settings(base, updates) if updates else base


__all__ = [
    "AppConfig",
    "LoadedSettings",
    "RuntimeEnv",
    "default_settings",
    "override_settings",
    "settings_from_flat",
]
