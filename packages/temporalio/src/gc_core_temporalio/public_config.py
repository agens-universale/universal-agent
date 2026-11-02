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
``completion_kwargs``, ``models.*.main``/``auxiliary``) проходят явную схему: неизвестный ключ — отказ, и текст
отказа значения не содержит. Явность полей сохраняется (``exclude_unset``):
workflow видит заданным ровно то, что задано у воркера.
"""

from __future__ import annotations

import dataclasses
import hashlib
import json
import math
from dataclasses import dataclass
from functools import lru_cache
from typing import Any, get_args

from gc_core.config_modules.field_meta import _nested_model, meta_for_parts
from gc_core.config_modules.os_backend import OsBackendKind
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
        "run_mode",
        "human_channel",
        "external_user_words",
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
    "stream_options": _fields("include_usage"),
    "stop": "scalar_or_list",
    # "auto" / "none" / {"type": "function", "function": {"name": …}}.
    "tool_choice": "json",
    "reasoning": _fields("effort max_tokens exclude enabled summary"),
    "thinking": _fields("type budget_tokens"),
    "chat_template_kwargs": _fields(
        "enable_thinking thinking_budget thinking reasoning_effort"
    ),
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
#: Неизвестный ключ (``extra_body``, ``api_key``…) остаётся на воркере: снимку
#: он не нужен — запрос к модели собирает activity из настроек воркера
#: (``ModelRouter(ctx)``), а расхождение приложения и воркера видит
#: ``settings_fingerprint`` (он считается по полным настройкам).
_FREE_FORM_POLICY = {
    ("backend", "chat_completion_kwargs"): _COMPLETION_PARAMETERS,
    # Параметры запроса алиаса (main — ход агента, auxiliary — служебные).
    ("models", "*", "main"): _COMPLETION_PARAMETERS,
    ("models", "*", "auxiliary"): _COMPLETION_PARAMETERS,
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


def _copy_scalar(value: Any, path: str) -> Any:
    if isinstance(value, (list, dict)):
        raise PublicConfigError(f"Expected a scalar public value at {path}")
    return _json_value(value, path)


def _copy_list(value: Any, item_schema: Any, path: str) -> list:
    if not isinstance(value, list):
        raise PublicConfigError(f"Expected a public list at {path}")
    return [_copy_policy(item, item_schema, f"{path}[]") for item in value]


def _copy_object(value: Any, schema: dict, path: str) -> dict:
    if not isinstance(value, dict):
        raise PublicConfigError(f"Expected a public object at {path}")
    output = {}
    for key, item in value.items():
        if not isinstance(key, str) or (
            key not in schema and "*" not in schema
        ):
            # Неизвестный параметр провайдера — только на воркере
            # (см. ``_FREE_FORM_POLICY``), в историю не идёт.
            continue
        output[key] = _copy_policy(
            item, schema.get(key, schema.get("*")), f"{path}.{key}"
        )
    return output


def _copy_policy(value: Any, schema: Any, path: str) -> Any:
    if schema == "json":
        return _json_value(value, path)
    if schema == "scalar_or_list":
        return _copy_policy(
            value, ["scalar"] if isinstance(value, list) else "scalar", path
        )
    if schema == "scalar":
        return _copy_scalar(value, path)
    if isinstance(schema, list):
        return _copy_list(value, schema[0], path)
    return _copy_object(value, schema, path)


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


#: Обязательные поля подключения ``os_backend`` по виду: сами они только для
#: воркера (``worker_only``), но без них снимок не собрать обратно в
#: настройки (валидатор требует их при этом ``kind``). Код workflow с бэкендом
#: ОС не работает (``ExternalResources``) — ему достаточно заглушки.
WORKER_ONLY_PLACEHOLDER = "worker-only"
_OS_BACKEND_REQUIRED = {
    "docker": ("docker", "name", WORKER_ONLY_PLACEHOLDER),
    "ssh": ("ssh", "host", WORKER_ONLY_PLACEHOLDER),
    "exec": ("exec", "argv_prefix", [WORKER_ONLY_PLACEHOLDER]),
}


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
    backend = document.get("os_backend")
    required = _OS_BACKEND_REQUIRED.get(
        settings.os_backend.kind if backend is not None else None
    )
    if required is not None:
        section, field, placeholder = required
        backend.setdefault(section, {})[field] = placeholder
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
    # Вид бэкенда — имя из схемы (секретов в нём нет); всё остальное о
    # бэкенде (адреса, ``env``) остаётся на воркере.
    if output.get("os_backend") not in (None, *get_args(OsBackendKind)):
        raise PublicConfigError("Invalid public backend kind")
    if output.get("run_mode") not in (None, "tui", "cli", "server"):
        raise PublicConfigError("Invalid public run mode")
    return output


def public_config_snapshot(source: Any) -> PublicConfigSnapshot:
    """Снимок настроек воркера для истории workflow.

    ``source`` — всё, что принимает ``settings_of``: контекст, ``LoadedSettings``,
    ``AppConfig``. Умолчания не дописываются, функции не выключаются: снимок —
    те же настройки без закрытых полей.
    """
    # Не на уровне модуля: модуль импортирует код workflow (песочница), а
    # снимок снимается только на воркере.
    from gc_core.model_router import consumer_has_transport

    settings, runtime = settings_of(source), runtime_of(source)
    url = settings.backend.completions_url
    return PublicConfigSnapshot(
        settings=public_settings(settings),
        runtime=public_runtime(runtime),
        model_transport_configured=consumer_has_transport(
            settings, "command_judge"
        ),
        provider_currency=provider_currency_for_url(url),
    )


#: Секции, которые у приложения и воркера законно разные: пути — свои у
#: процесса (абсолютные, от каталога конфига), логирование, ACP и цены — на
#: поведение хода не влияют.
_FINGERPRINT_SKIP = frozenset({"paths", "logging", "acp", "pricing"})
#: Из ``tui`` на ход влияет только это (AGENTS.md в промпте); режимы ответа и
#: судьи хода TUI передаёт через ``RuntimeEnv``, а не настройками.
_FINGERPRINT_TUI_KEYS = frozenset({"agentsmd_override"})
#: Листья с путями от каталога конфига вне ``paths``: у процессов разные.
_FINGERPRINT_SKIP_LEAVES = (("security", "write_guard", "extra_paths"),)


def _without_local_paths(value):
    """Без путей промптов (``*_prompt_path``): абсолютные от каталога
    конфига, у приложения и воркера разные при одинаковых настройках."""
    if isinstance(value, dict):
        return {
            key: _without_local_paths(item)
            for key, item in value.items()
            if not str(key).endswith("_prompt_path")
        }
    return value


def settings_fingerprint(source: Any) -> dict[str, str]:
    """Отпечаток настроек агента по секциям: ``{секция: sha256[:16]}``.

    Сравнивает настройки приложения (``AsyncAgent``/``AgentSession``) и
    профиля воркера: ход идёт по профилю воркера, и разошедшиеся настройки
    значили бы, что агент ведёт себя не так, как приложение ожидает. Полные
    значения (с умолчаниями — явно заданное и умолчание с тем же значением не
    различаются), без закрытых полей; наружу уходят только хэши.
    """
    # Нормализация лимитов summary — та же, что при загрузке YAML
    # (``resolve_paths``): ``AppConfig`` из JSON или кода без неё держит
    # ``None`` там, где загрузка ставит производные значения (0 и пр.) —
    # поведение одинаковое, а отпечаток расходился бы.
    from gc_core.config_modules.editing import editable
    from gc_core.config_modules.root import _normalize_summary_limits

    edit = editable(settings_of(source))
    _normalize_summary_limits(edit)
    settings = edit.result()
    document = _without_local_paths(
        settings.model_dump(
            mode="json", by_alias=True, exclude=settings_exclude_spec()
        )
    )
    if isinstance(document.get("tui"), dict):
        document["tui"] = {
            key: value
            for key, value in document["tui"].items()
            if key in _FINGERPRINT_TUI_KEYS
        }
    for path in _FINGERPRINT_SKIP_LEAVES:
        parent = document
        for key in path[:-1]:
            parent = parent.get(key) if isinstance(parent, dict) else None
        if isinstance(parent, dict):
            parent.pop(path[-1], None)
    return {
        section: hashlib.sha256(
            json.dumps(value, sort_keys=True, ensure_ascii=False).encode()
        ).hexdigest()[:16]
        for section, value in sorted(document.items())
        if section not in _FINGERPRINT_SKIP
    }


def fingerprint_mismatch(expected: dict, actual: dict) -> list[str]:
    """Секции, где отпечатки расходятся (есть только у одной стороны — тоже)."""
    return sorted(
        section
        for section in {*expected, *actual}
        if expected.get(section) != actual.get(section)
    )


__all__ = [
    "PublicConfigError",
    "PublicConfigSnapshot",
    "fingerprint_mismatch",
    "is_private_path",
    "public_config_snapshot",
    "public_runtime",
    "public_settings",
    "settings_exclude_spec",
    "settings_fingerprint",
]
