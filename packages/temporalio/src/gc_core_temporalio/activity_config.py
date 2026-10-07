"""Таймауты и повторы activities — настраиваются, как ``ActivityConfig`` у Pydantic AI.

Уровни накладываются по порядку: умолчания фреймворка → ``activities.default``
→ уровень (``model``, ``resource``, ``thread_store``) → ``activities.tools.<имя>``
для инструмента. Конфиг проверяется на старте воркера: ошибка в нём иначе
всплыла бы внутри workflow вечным сбоем workflow task.

Фреймворк всегда дописывает свои неповторяемые типы (доменные отказы и
превышение размера payload — повтор их не исправит). Эффект инструмента
(запись, команда, MCP) по умолчанию исполняется одной попыткой: Temporal
повторяет activity «хотя бы раз», и после падения воркера повтор исполнил бы
эффект второй раз. Модель вместо повтора получает «исход неизвестен,
проверьте состояние». Повторы эффекта включаются явно —
``activities.tools.<имя>.retry.maximum_attempts`` (тогда эффект может
выполниться повторно).

В workflow настройки приходят данными из ``agent.context.prepare``: так код
workflow остаётся детерминированным, а смена конфига действует со следующего
хода.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

#: Типы, которые повторять бессмысленно, — дописываются в любую политику.
NON_RETRYABLE_ERROR_TYPES = (
    "DomainError",
    "ExecutionFailure",
    "InfrastructureFailure",
    "OutcomeUnknown",
    "ToolOutcomeUnknown",
    # Превышение лимита payload детерминировано: повтор отправил бы тот же
    # результат снова (имя зависит от версии SDK).
    "PayloadsTooLarge",
    "PayloadSizeError",
)

#: Эффект по умолчанию — одна попытка (не больше одного исполнения).
EFFECT_ATTEMPTS = 1


class RetrySettings(BaseModel):
    """Поля ``temporalio.common.RetryPolicy``; ``maximum_attempts: 0`` — без ограничения."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    initial_interval: timedelta | None = None
    backoff_coefficient: float | None = Field(default=None, ge=1)
    maximum_interval: timedelta | None = None
    maximum_attempts: int | None = Field(default=None, ge=0)
    non_retryable_error_types: list[str] = Field(default_factory=list)


class ActivitySettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)
    start_to_close: timedelta | None = None
    schedule_to_close: timedelta | None = None
    heartbeat_timeout: timedelta | None = None
    retry: RetrySettings | None = None


class ActivitiesConfig(BaseModel):
    """Секция ``activities`` конфига воркера."""

    model_config = ConfigDict(extra="forbid", frozen=True)
    default: ActivitySettings = Field(default_factory=ActivitySettings)
    #: ``agent.model.request``: сбои провайдера (обрыв, 5xx, 429) Temporal
    #: повторяет по этой политике — ход ждёт, а не падает.
    model: ActivitySettings = Field(default_factory=ActivitySettings)
    #: Подготовка, guards, промпты, чтения, память, субагенты.
    resource: ActivitySettings = Field(default_factory=ActivitySettings)
    #: ``agent.thread.*``.
    thread_store: ActivitySettings = Field(default_factory=ActivitySettings)
    #: Инструмент по имени (``exec_command``) поверх ``resource``.
    tools: dict[str, ActivitySettings] = Field(default_factory=dict)


_FRAMEWORK = {
    "model": ActivitySettings(
        start_to_close=timedelta(minutes=15),
        heartbeat_timeout=timedelta(seconds=30),
        # Как Temporal-умолчание у Pydantic AI: ждать провайдера сколько
        # угодно, с паузой до минуты. Отказ вместо ожидания — schedule_to_close
        # или maximum_attempts в конфиге.
        retry=RetrySettings(
            initial_interval=timedelta(seconds=1),
            backoff_coefficient=2,
            maximum_interval=timedelta(minutes=1),
            maximum_attempts=0,
        ),
    ),
    "resource": ActivitySettings(
        start_to_close=timedelta(minutes=15),
        heartbeat_timeout=timedelta(seconds=30),
        retry=RetrySettings(maximum_attempts=5),
    ),
    "thread_store": ActivitySettings(
        start_to_close=timedelta(minutes=2),
        retry=RetrySettings(maximum_attempts=5),
    ),
}


def _merge(*layers: ActivitySettings | None) -> dict[str, Any]:
    out: dict[str, Any] = {}
    retry: dict[str, Any] = {}
    for layer in layers:
        if layer is None:
            continue
        for name in (
            "start_to_close",
            "schedule_to_close",
            "heartbeat_timeout",
        ):
            value = getattr(layer, name)
            if value is not None:
                out[name] = value.total_seconds()
        if layer.retry is not None:
            for name, value in layer.retry.model_dump(
                exclude_unset=True
            ).items():
                if isinstance(value, timedelta):
                    value = value.total_seconds()
                retry[name] = value
    types = list(retry.get("non_retryable_error_types") or [])
    retry["non_retryable_error_types"] = [
        *types,
        *(name for name in NON_RETRYABLE_ERROR_TYPES if name not in types),
    ]
    out["retry"] = retry
    return out


def resolve_activity_settings(
    config: ActivitiesConfig | None,
) -> dict[str, Any]:
    """Итоговые настройки по уровням — данные для workflow (секунды, без
    pydantic): ``{"model": …, "resource": …, "thread_store": …, "tools": {…}}``."""
    config = config or ActivitiesConfig()
    resolved = {
        level: _merge(
            _FRAMEWORK[level], config.default, getattr(config, level)
        )
        for level in ("model", "resource", "thread_store")
    }
    resolved["tools"] = {}
    for name, settings in config.tools.items():
        merged = _merge(
            _FRAMEWORK["resource"], config.default, config.resource, settings
        )
        if (
            settings.retry is not None
            and "maximum_attempts" in settings.retry.model_fields_set
        ):
            # Повторы эффекта этого инструмента включены явно.
            merged["effect_attempts"] = settings.retry.maximum_attempts
        resolved["tools"][name] = merged
    return resolved


#: Умолчания до первого ``agent.context.prepare`` хода.
DEFAULT_ACTIVITY_SETTINGS = resolve_activity_settings(None)


def activity_options(settings: dict[str, Any], *, effect: bool = False):
    """Аргументы ``workflow.execute_activity`` из настроек уровня (в workflow)."""
    from temporalio.common import RetryPolicy

    retry = dict(settings.get("retry") or {})
    if effect:
        retry["maximum_attempts"] = settings.get(
            "effect_attempts", EFFECT_ATTEMPTS
        )
    options: dict[str, Any] = {
        "retry_policy": RetryPolicy(
            initial_interval=timedelta(
                seconds=retry.get("initial_interval", 1)
            ),
            backoff_coefficient=retry.get("backoff_coefficient", 2.0),
            maximum_interval=(
                timedelta(seconds=retry["maximum_interval"])
                if retry.get("maximum_interval") is not None
                else None
            ),
            maximum_attempts=retry.get("maximum_attempts", 0),
            non_retryable_error_types=list(
                retry.get("non_retryable_error_types") or []
            ),
        )
    }
    for name, key in (
        ("start_to_close", "start_to_close_timeout"),
        ("schedule_to_close", "schedule_to_close_timeout"),
        ("heartbeat_timeout", "heartbeat_timeout"),
    ):
        if settings.get(name) is not None:
            options[key] = timedelta(seconds=settings[name])
    return options


__all__ = [
    "DEFAULT_ACTIVITY_SETTINGS",
    "EFFECT_ATTEMPTS",
    "NON_RETRYABLE_ERROR_TYPES",
    "ActivitiesConfig",
    "ActivitySettings",
    "RetrySettings",
    "activity_options",
    "resolve_activity_settings",
]
