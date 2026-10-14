"""Meta develop (``meta_<s>_<t>.json``) ↔ модели хранилища тредов v2.

Файловое хранилище пишет модели в те же ключи, что develop, — без миграции и
с безопасным откатом. Отображение обратимо без потерь:

* **ключ есть в meta ⇔ поле модели задано явно** (``model_fields_set``):
  develop пишет часть ключей условно (``project_memory_check_cnt``,
  системный промпт, флаги ``runtime``), и «нет ключа» не равно «null»;
* значение, которое модель не принимает (старая meta с чужим типом), не
  роняет чтение треда: ключ уходит в ``extra`` как есть;
* ключи, которых модель не знает, лежат в ``extra`` и пишутся обратно;
* порядок ключей берётся у хранимой meta (``base``), новые — в конец.

Служебные ключи ставит хранилище: штампы (``system``, ``thread_id``,
``schema_version``, ``state_schema_version``), ``created_at`` /
``updated_at``, ``messages_count``, ``last_summary_archive_suffix``.
"""

from __future__ import annotations

import copy
from dataclasses import dataclass, replace
from typing import Any

from pydantic import BaseModel, ValidationError

from gc_core.persistence.schema import STATE_SCHEMA_VERSION
from gc_core.threads.models import (
    JsonObject,
    RuntimeStatus,
    SessionState,
    ThreadInfo,
    Usage,
)

_ABSENT = object()

#: Поле SessionState → путь ключа develop. Порядок — как пишет develop.
_STATE_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("previous_response_id", ("previous_response_id",)),
    ("working_memory", ("shared", "working_memory")),
    ("notes", ("shared", "notes_md")),
    ("fetch_url_cache", ("shared", "fetch_url_cache")),
    ("confirm_rules", ("shared", "confirm_rules")),
    ("todo", ("shared", "session_todo")),
    ("intent_log", ("user_intent_log",)),
    ("current_intent", ("current_user_intent",)),
    ("user_turn_seq", ("user_turn_seq",)),
    ("dialog_steps_offset", ("dialog_steps_offset",)),
    ("last_prompt_tokens", ("last_main_prompt_tokens",)),
    ("last_prompt_message_count", ("last_main_prompt_message_count",)),
    ("rendered_system_prompt", ("chat_completions_system_prompt_rendered",)),
    ("project_memory_version", ("project_memory_content_hash",)),
    ("tool_usage_notice", ("tool_usage_notice_cnt",)),
    ("todo_reminder_count", ("todo_reminder_cnt",)),
    ("project_memory_check_count", ("project_memory_check_cnt",)),
    ("last_backend_response_id", ("last_backend_response_id",)),
    ("last_summarized_at", ("last_summarized_at",)),
)

_INFO_KEYS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("created_at", ("created_at",)),
    ("updated_at", ("updated_at",)),
    ("api_mode", ("backend_api_mode",)),
    ("messages_count", ("messages_count",)),
    ("workspace", ("workspace",)),
    ("title", ("tui", "title")),
)

_RUNTIME_FIELDS = (
    "turn",
    "live_msg_count",
    "live_history_chars",
    "image_seq",
    "mcp_tools",
    "trace_ids",
)
_RUN_FLAGS = ("summarizing", "finalizing", "finish_gate_checking")

_USAGE_KEY = "token_usage"
_RUNTIME_KEY = "runtime"
_TUI_KEY = "tui"
_FRONTENDS_KEY = "frontends"
#: Ключи, у которых есть место в модели, — остальное идёт в ``extra``.
_MAPPED_TOP = frozenset(
    {path[0] for _, path in (*_STATE_KEYS, *_INFO_KEYS)}
    | {_USAGE_KEY, _RUNTIME_KEY, _TUI_KEY, _FRONTENDS_KEY}
)
#: Ключ, который ставит само хранилище при архивации истории.
ARCHIVE_SUFFIX_KEY = "last_summary_archive_suffix"


@dataclass(frozen=True)
class MetaParts:
    """Meta треда, разложенная по моделям контракта."""

    info: ThreadInfo
    state: SessionState
    usage: Usage
    runtime: RuntimeStatus
    frontend: dict[str, JsonObject]
    extra: JsonObject

    def with_(self, **changes: Any) -> MetaParts:
        return replace(self, **changes)


# --- meta → модели ------------------------------------------------------------


def _get(meta: JsonObject, path: tuple[str, ...]) -> Any:
    node: Any = meta
    for part in path:
        if not isinstance(node, dict) or part not in node:
            return _ABSENT
        node = node[part]
    return node


def _model(cls: type[BaseModel], values: JsonObject, rejected: JsonObject):
    """Модель с явными полями ровно из ``values``; негодные — в ``rejected``."""
    accepted: JsonObject = {}
    for name, value in values.items():
        try:
            cls.model_validate({name: value})
        except ValidationError:
            rejected[name] = value
            continue
        accepted[name] = value
    validated = cls.model_validate(accepted)
    return validated.model_copy() if accepted else cls()


def _collect(meta: JsonObject, keys) -> tuple[JsonObject, JsonObject]:
    """Поля по таблице ключей и пути, по которым они найдены."""
    values: JsonObject = {}
    paths: JsonObject = {}
    for field, path in keys:
        value = _get(meta, path)
        if value is not _ABSENT:
            values[field] = value
            paths[field] = path
    return values, paths


def _info(meta: JsonObject, rejected: dict) -> ThreadInfo:
    values, paths = _collect(meta, _INFO_KEYS)
    bad: JsonObject = {}
    info = _model(ThreadInfo, values, bad)
    for field in bad:
        rejected[paths[field]] = bad[field]
    if info.workspace is None:
        project = _get(meta, (_TUI_KEY, "project_path"))
        if isinstance(project, str):
            # Значение есть, а ключа ``workspace`` нет: поле не помечается
            # заданным, чтобы обратная запись не добавила ключ.
            info = ThreadInfo.model_construct(
                _fields_set=set(info.model_fields_set),
                **{**info.__dict__, "workspace": project},
            )
    return info


def _state(meta: JsonObject, rejected: dict) -> SessionState:
    values, paths = _collect(meta, _STATE_KEYS)
    bad: JsonObject = {}
    state = _model(SessionState, values, bad)
    for field in bad:
        rejected[paths[field]] = bad[field]
    return state


def _runtime(meta: JsonObject, rejected: dict) -> RuntimeStatus:
    raw = meta.get(_RUNTIME_KEY, _ABSENT)
    if raw is _ABSENT:
        return RuntimeStatus()
    if not isinstance(raw, dict):
        rejected[(_RUNTIME_KEY,)] = raw
        return RuntimeStatus()
    if not raw:
        # Пустая секция develop (update_runtime_meta её оставляет): поле
        # ``flags`` помечается заданным — так запись знает, что секция есть.
        return RuntimeStatus.model_construct(_fields_set={"flags"})
    values: JsonObject = {}
    flags = []
    for key, value in raw.items():
        if key in _RUN_FLAGS and value is True:
            flags.append(key)
        else:
            values[key] = value
    if flags:
        values["flags"] = frozenset(flags)
    bad: JsonObject = {}
    runtime = _model(RuntimeStatus, values, bad)
    for key, value in bad.items():
        rejected[(_RUNTIME_KEY, key)] = value
    return runtime


def _frontend(meta: JsonObject) -> dict[str, JsonObject]:
    frontend: dict[str, JsonObject] = {}
    tui = meta.get(_TUI_KEY)
    if isinstance(tui, dict):
        frontend["tui"] = {k: v for k, v in tui.items() if k != "title"}
    others = meta.get(_FRONTENDS_KEY)
    if isinstance(others, dict):
        frontend.update(copy.deepcopy(others))
    return frontend


def parts_from_meta(meta: JsonObject) -> MetaParts:
    """Разложить meta develop по моделям; ничего не теряется."""
    rejected: dict[tuple[str, ...], Any] = {}
    info = _info(meta, rejected)
    state = _state(meta, rejected)
    runtime = _runtime(meta, rejected)
    usage = Usage()
    raw_usage = meta.get(_USAGE_KEY, _ABSENT)
    if raw_usage is not _ABSENT:
        try:
            usage = Usage.model_validate(raw_usage)
        except ValidationError:
            rejected[(_USAGE_KEY,)] = raw_usage
    extra = {
        key: copy.deepcopy(value)
        for key, value in meta.items()
        if key not in _MAPPED_TOP
    }
    # Негодное значение возвращается на своё место как есть.
    extra["__rejected__"] = [
        [list(path), copy.deepcopy(value)] for path, value in rejected.items()
    ]
    if not extra["__rejected__"]:
        del extra["__rejected__"]
    return MetaParts(
        info=info,
        state=state,
        usage=usage,
        runtime=runtime,
        frontend=_frontend(meta),
        extra=extra,
    )


# --- модели → meta ------------------------------------------------------------


def _put(meta: JsonObject, path: tuple[str, ...], value: Any) -> None:
    node = meta
    for part in path[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[path[-1]] = copy.deepcopy(value)


def _dump(model: BaseModel, field: str) -> Any:
    return model.model_dump(mode="json", include={field})[field]


def _write_fields(meta: JsonObject, model: BaseModel, keys) -> None:
    for field, path in keys:
        if field in model.model_fields_set:
            _put(meta, path, _dump(model, field))


def _write_runtime(meta: JsonObject, runtime: RuntimeStatus) -> None:
    values: JsonObject = {}
    for field in _RUNTIME_FIELDS:
        if field in runtime.model_fields_set:
            values[field] = _dump(runtime, field)
    for flag in sorted(runtime.flags):
        values[flag] = True
    values.update(copy.deepcopy(runtime.model_extra or {}))
    if values or runtime.model_fields_set:
        meta[_RUNTIME_KEY] = values


def _write_frontend(meta: JsonObject, parts: MetaParts) -> None:
    tui = parts.frontend.get("tui")
    title_set = "title" in parts.info.model_fields_set
    if tui is not None or title_set:
        merged: JsonObject = {}
        if title_set:
            merged["title"] = parts.info.title
        merged.update(copy.deepcopy(tui or {}))
        meta[_TUI_KEY] = merged
    others = {k: v for k, v in parts.frontend.items() if k != "tui"}
    if others:
        meta[_FRONTENDS_KEY] = copy.deepcopy(others)


def _ordered_like(new: Any, base: Any) -> Any:
    """Порядок ключей как у ``base`` (рекурсивно), новые ключи — в конец."""
    if not isinstance(new, dict) or not isinstance(base, dict):
        return new
    ordered = {k: _ordered_like(new[k], base[k]) for k in base if k in new}
    ordered.update({k: v for k, v in new.items() if k not in base})
    return ordered


def meta_from_parts(parts: MetaParts, *, base: JsonObject) -> JsonObject:
    """Собрать meta develop из моделей; ``base`` — хранимая meta (порядок)."""
    meta: JsonObject = {}
    extra = copy.deepcopy(parts.extra)
    # Отвергнутые моделью значения — первыми: заданное поле их перекроет.
    for path, value in extra.pop("__rejected__", []):
        _put(meta, tuple(path), value)
    _write_fields(meta, parts.info, _INFO_KEYS[:5])
    _write_runtime(meta, parts.runtime)
    _write_fields(meta, parts.state, _STATE_KEYS)
    if parts.usage.model_fields_set or parts.usage.model_extra:
        meta[_USAGE_KEY] = parts.usage.model_dump(mode="json")
    _write_frontend(meta, parts)
    for key, value in extra.items():
        meta[key] = value
    return _ordered_like(meta, base)


def meta_from_record(record) -> JsonObject:
    """Meta develop из записи треда — то, что старый код видит как meta.

    Значения, которые ставит хранилище (``created_at``, ``updated_at``,
    ``messages_count``), берутся из ``record.info``; служебные ключи файлового
    хранилища (штампы, суффикс архива) уже лежат в ``record.extra``.
    """
    info = record.info.model_copy(
        update={
            "created_at": record.info.created_at,
            "updated_at": record.info.updated_at,
            "messages_count": record.info.messages_count,
        }
    )
    parts = MetaParts(
        info=info,
        state=record.state,
        usage=record.usage,
        runtime=record.runtime,
        frontend=record.frontend,
        extra=record.extra,
    )
    meta = meta_from_parts(parts, base={})
    for key in ("created_at", "updated_at"):
        if meta.get(key) is None:
            meta.pop(key, None)
    # Инвариант develop: meta с состоянием агента несёт версию схемы, иначе
    # загрузка треда откажет (validate_thread_meta_for_load). Файловое
    # хранилище хранит штамп само; для остальных он ставится здесь.
    meta.setdefault("state_schema_version", STATE_SCHEMA_VERSION)
    return meta
