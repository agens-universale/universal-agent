"""Публичный снимок настроек для durable-истории (Temporal).

Снимок — вложенный JSON-дамп типизированных ``AppConfig`` и ``RuntimeEnv``
воркера, а не плоского вида. Workflow собирает из него те же настройки
(``gc_core.context_preparation.settings_from_snapshot``) без ``resolve_paths``.

В снимок не попадают:

- поля схемы с флагом ``worker_only`` или ``secret`` (``gc_meta``, флаг секции —
  у всех её листьев): адреса, учётные данные, заголовки, прокси, подключения
  MCP, настройки фронтенда;
- адрес записи реестра моделей (``models.*.url``): workflow не знает, куда
  уходит запрос, — транспорт резолвит воркер;
- рантайм воркера, который workflow не нужен: пути конфига, notices,
  managed-признаки.

Произвольные словари параметров провайдера (``chat_completion_kwargs``,
``completion_kwargs``) проходят явную схему: неизвестный ключ — отказ, и текст
отказа значения не содержит. Явность полей сохраняется (``exclude_unset``):
workflow видит заданным ровно то, что задано у воркера.
"""

from __future__ import annotations

import dataclasses
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any

from gc_core.config_modules.field_meta import _nested_model, meta_for_parts
from gc_core.config_modules.root import AppConfig
from gc_core.settings import RuntimeEnv, runtime_of, settings_of
from gc_core.usage.currency import provider_currency_for_url
from pydantic import BaseModel


class PublicConfigError(ValueError):
    pass


@dataclass(frozen=True)
class PublicConfigSnapshot:
    #: Вложенный дамп ``AppConfig`` (ключи — алиасы полей) без закрытых полей.
    settings: dict[str, Any]
    #: Публичная часть ``RuntimeEnv`` (кортежи — списками).
    runtime: dict[str, Any]
    model_transport_configured: bool
    provider_currency: str
    model_provider_currencies: dict[str, str]


#: Поля схемы, закрытые политикой снимка, а не флагом: адрес записи реестра —
#: не секрет для показа, но в историю workflow он не уходит, как и общий
#: ``backend.completions_url``. Пара «адрес + токен» уходит из записи целиком —
#: правило пары ``ModelEntry`` на снимке выполняется.
_POLICY_PRIVATE_PATHS = frozenset({"models.*.url"})


def is_private_path(parts: tuple[str, ...]) -> bool:
    """Путь схемы (сегменты, ``*`` — запись словаря) не идёт в снимок."""
    meta = meta_for_parts(list(parts))
    return (
        meta.worker_only
        or meta.secret
        or ".".join(parts) in _POLICY_PRIVATE_PATHS
    )


def _exclude_spec(
    model: type[BaseModel], prefix: tuple[str, ...] = ()
) -> dict[str, Any]:
    """``exclude`` для ``model_dump``: закрытые поля по метаданным схемы."""
    spec: dict[str, Any] = {}
    for name, info in model.model_fields.items():
        parts = (*prefix, name)
        if is_private_path(parts):
            spec[name] = True
            continue
        kind, nested = _nested_model(info.annotation)
        if kind == "model":
            sub = _exclude_spec(nested, parts)
            if sub:
                spec[name] = sub
        elif kind == "dict":
            sub = _exclude_spec(nested, (*parts, "*"))
            if sub:
                spec[name] = {"__all__": sub}
    return spec


@lru_cache(maxsize=1)
def settings_exclude_spec() -> dict[str, Any]:
    """Закрытые поля ``AppConfig`` — одна спецификация на снимок и проверки."""
    return _exclude_spec(AppConfig)


#: Рантайм: каждое поле ``RuntimeEnv`` классифицировано явно — новое поле без
#: классификации роняет снимок, а не уходит в историю молча.
_RUNTIME_PUBLIC = frozenset(
    {
        "active_profile",
        "launch_root",
        "project_root",
        "trace_id",
        "is_tui",
        "text_answer_mode",
        "finish_gate_smart",
        "subagent_tools_allow",
        "subagent_tools_stop",
        "os_backend",
    }
)
_RUNTIME_WORKER_ONLY = frozenset(
    {"config_path", "config_dir", "notices", "voice_overridden_by_managed"}
)


def _fields(names: str) -> dict[str, str]:
    return dict.fromkeys(names.split(), "scalar")


_COMPLETION_PARAMETERS = {
    **_fields(
        "stream temperature top_p top_k min_p frequency_penalty presence_penalty repetition_penalty max_tokens max_completion_tokens reasoning_effort seed logprobs top_logprobs n parallel_tool_calls store verbosity enable_thinking thinking_budget max_output_tokens"
    ),
    "stop": "scalar_or_list",
    "reasoning": _fields("effort max_tokens exclude enabled summary"),
    "thinking": _fields("type budget_tokens"),
    "chat_template_kwargs": _fields("enable_thinking thinking_budget"),
    "response_format": {
        **_fields("type"),
        "json_schema": {
            **_fields("name description strict"),
            "schema": "json",
        },
    },
}

#: Произвольные словари параметров провайдера: туда кладут что угодно, в том
#: числе ключи и заголовки, — в снимок идут только известные параметры.
_FREE_FORM_POLICY = {
    ("backend", "chat_completion_kwargs"): _COMPLETION_PARAMETERS,
    (
        "models",
        "*",
        "backend",
        "chat_completion_kwargs",
    ): _COMPLETION_PARAMETERS,
    (
        "chat_completions",
        "auxiliary_llm",
        "completion_kwargs",
    ): _COMPLETION_PARAMETERS,
}


def _json_value(value: Any, path: str) -> Any:
    if value is None or isinstance(value, (str, bool, int)):
        return value
    if isinstance(value, float) and math.isfinite(value):
        return value
    if isinstance(value, list):
        return [_json_value(item, f"{path}[]") for item in value]
    if isinstance(value, dict) and all(isinstance(key, str) for key in value):
        return {
            key: _json_value(item, f"{path}.{key}")
            for key, item in value.items()
        }
    raise PublicConfigError(f"Non-JSON public value at {path}")


def _copy_policy(value: Any, schema: Any, path: str) -> Any:
    if schema == "json":
        return _json_value(value, path)
    if schema == "scalar_or_list":
        return _copy_policy(
            value, ["scalar"] if isinstance(value, list) else "scalar", path
        )
    if schema == "scalar":
        if isinstance(value, (list, dict)):
            raise PublicConfigError(
                f"Expected a scalar public value at {path}"
            )
        return _json_value(value, path)
    if isinstance(schema, list):
        if not isinstance(value, list):
            raise PublicConfigError(f"Expected a public list at {path}")
        return [_copy_policy(item, schema[0], f"{path}[]") for item in value]
    if not isinstance(value, dict):
        raise PublicConfigError(f"Expected a public object at {path}")
    output = {}
    for key, item in value.items():
        if not isinstance(key, str) or (
            key not in schema and "*" not in schema
        ):
            raise PublicConfigError(
                f"Unclassified public field at {path}.{key}"
            )
        output[key] = _copy_policy(
            item, schema.get(key, schema.get("*")), f"{path}.{key}"
        )
    return output


def _apply_free_form_policy(
    document: Any, parts: tuple[str, ...], pattern: tuple[str, ...], schema
) -> None:
    """Пропустить словарь по пути ``pattern`` (``*`` — любая запись) через схему."""
    if not isinstance(document, dict) or not pattern:
        return
    head, *rest = pattern
    keys = list(document) if head == "*" else [head]
    for key in keys:
        if key not in document:
            continue
        path = (*parts, key)
        if rest:
            _apply_free_form_policy(document[key], path, tuple(rest), schema)
        else:
            document[key] = _copy_policy(document[key], schema, ".".join(path))


def public_settings(settings: AppConfig) -> dict[str, Any]:
    """Вложенный JSON-дамп настроек без закрытых полей (ключи — алиасы)."""
    document = settings.model_dump(
        mode="json",
        by_alias=True,
        exclude_unset=True,
        exclude=settings_exclude_spec(),
    )
    for pattern, schema in _FREE_FORM_POLICY.items():
        _apply_free_form_policy(document, (), pattern, schema)
    return _json_value(document, "settings")


def public_runtime(runtime: RuntimeEnv) -> dict[str, Any]:
    """Публичная часть рантайма; новое поле ``RuntimeEnv`` требует классификации."""
    output = {}
    for field in dataclasses.fields(runtime):
        name = field.name
        if name in _RUNTIME_WORKER_ONLY:
            continue
        if name not in _RUNTIME_PUBLIC:
            raise PublicConfigError(f"Unclassified runtime field: {name}")
        value = getattr(runtime, name)
        if isinstance(value, tuple):
            value = list(value)
        output[name] = _json_value(value, f"runtime.{name}")
    if output.get("os_backend") not in (None, "local", "harbor"):
        raise PublicConfigError("Invalid public backend kind")
    return output


def _provider_currencies(settings: AppConfig) -> dict[str, str]:
    inherited = settings.backend.completions_url
    return {
        alias: provider_currency_for_url(
            entry.url
            or (entry.backend.completions_url if entry.backend else None)
            or inherited
        )
        for alias, entry in settings.models.items()
    }


def public_config_snapshot(source: Any) -> PublicConfigSnapshot:
    """Снимок настроек воркера для истории workflow.

    ``source`` — всё, что принимает ``settings_of``: контекст, ``LoadedSettings``,
    ``AppConfig``. Умолчания не дописываются, функции не выключаются: снимок —
    те же настройки без закрытых полей.
    """
    settings, runtime = settings_of(source), runtime_of(source)
    url = settings.backend.completions_url
    return PublicConfigSnapshot(
        settings=public_settings(settings),
        runtime=public_runtime(runtime),
        model_transport_configured=bool(url),
        provider_currency=provider_currency_for_url(url),
        model_provider_currencies=_provider_currencies(settings),
    )


__all__ = [
    "PublicConfigError",
    "PublicConfigSnapshot",
    "is_private_path",
    "public_config_snapshot",
    "public_runtime",
    "public_settings",
    "settings_exclude_spec",
]
