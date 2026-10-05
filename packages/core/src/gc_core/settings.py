"""Настройки агента: неизменяемая модель, рантайм-окружение и их происхождение.

``AppConfig`` — то, что задал пользователь (файлы, слои, env, CLI, сессия);
модели неизменяемы, правка — ``override_settings`` / ``LoadedSettings.with_overrides``
даёт новый провалидированный объект. ``RuntimeEnv`` — то, что вычисляет запуск
(каталог старта, активный профиль, notices), а не пользователь.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from typing import Any, Mapping, Optional

from pydantic import BaseModel

from gc_core.config_modules.field_meta import _nested_model
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
    #: Режим запуска, объявленный фронтендом: ``tui``/``cli``/``server`` (от него
    #: зависит запись журнала правок, ``agent_edits.record``); None — ядро как
    #: библиотека.
    run_mode: Optional[str] = None

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
        # Словарь моделей проверяется раньше вложенной модели: у
        # ``dict[str, Model]`` модель лежит в аргументах аннотации, и иначе
        # ключ записи (сервер MCP) съедался бы как поле.
        kind, nested = _nested_model(info.annotation)
        current = _DictOf(nested) if kind == "dict" else nested
    return keys


class _DictOf:
    """Маркер «следующий сегмент — ключ словаря моделей ``value_type``»."""

    def __init__(self, value_type: type[BaseModel]):
        self.value_type = value_type


def _replace_at(document: dict[str, Any], dotted: str, value: Any) -> None:
    """Поставить ``value`` по dotted-пути, заменив прежнее значение целиком."""
    keys = _alias_path(AppConfig, dotted.split("."))
    target = document
    for key in keys[:-1]:
        nested = target.get(key)
        if not isinstance(nested, dict):
            nested = target[key] = {}
        target = nested
    target[keys[-1]] = value


def _nested(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Документ из dotted-правок (``{"agent.max_steps": 5}`` → вложенный)."""
    document: dict[str, Any] = {}
    for dotted, value in updates.items():
        _replace_at(document, dotted, value)
    return document


def _is_section(dotted: str) -> bool:
    """Путь ведёт к вложенной модели-секции (``summary``), а не к листу."""
    current: Any = AppConfig
    for name in dotted.split("."):
        fields = getattr(current, "model_fields", None) or {}
        info = fields.get(name) or next(
            (field for field in fields.values() if field.alias == name), None
        )
        if info is None:
            return False
        kind, nested = _nested_model(info.annotation)
        if kind != "model":
            return False
        current = nested
    return True


def expand_section_updates(updates: Mapping[str, Any]) -> dict[str, Any]:
    """Правки-секции (словарь на месте секции схемы) → правки её листьев.

    ``override_settings`` заменяет значение по пути. Слой, который должен
    СЛИВАТЬСЯ со значениями ниже (профиль бенча поверх его умолчаний),
    раскладывает секции на листья: заданное заменяется, прочее остаётся.
    Словари-листья (``chat_completion_kwargs``) и записи словарей моделей
    (``models.<alias>``) остаются целыми.
    """
    out: dict[str, Any] = {}

    def expand(dotted: str, value: Any) -> None:
        if isinstance(value, Mapping) and _is_section(dotted):
            for key, item in value.items():
                expand(f"{dotted}.{key}", item)
        else:
            out[dotted] = value

    for dotted, value in updates.items():
        expand(dotted, value)
    return out


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

    Правка ЗАМЕНЯЕТ значение по своему пути, а не сливается с ним: CLI,
    сервер и сессия сужают списки и словари (``--mcp-servers '{}'``
    отключает серверы, ``allowed_read_roots`` не расширяется файловым
    значением). Объединение списков — только у файловых слоёв.
    """
    if not updates:
        return settings
    merged = settings.model_dump(by_alias=True, exclude_unset=True)
    for dotted, value in updates.items():
        _replace_at(merged, dotted, value)
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


def _loaded_of(source: Any) -> LoadedSettings:
    """LoadedSettings из контекста рана, ``LoadedSettings`` или ``AppConfig``."""
    if isinstance(source, LoadedSettings):
        return source
    if isinstance(source, AppConfig):
        return LoadedSettings(settings=source)
    if source is None:
        return LoadedSettings(settings=AppConfig())
    settings = getattr(source, "settings", None)
    if isinstance(settings, LoadedSettings):
        return settings
    if isinstance(settings, AppConfig):
        runtime = getattr(source, "runtime", None)
        return LoadedSettings(
            settings=settings,
            runtime=runtime
            if isinstance(runtime, RuntimeEnv)
            else RuntimeEnv(),
        )
    raise TypeError(
        "ожидались настройки (контекст рана, LoadedSettings, AppConfig), "
        f"а не {type(source).__name__}"
    )


def settings_of(source: Any) -> AppConfig:
    """Настройки (``AppConfig``) из ``RunContext``, ``LoadedSettings``, ``AppConfig``.

    Единая точка чтения для потребителей; ``None`` — умолчания. Плоский словарь
    не принимается: ``TypeError``.
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
    "settings_of",
]
