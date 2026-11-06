"""Состояние агента (``State``) ↔ части записи треда v2.

Один перевод вместо прежних разрозненных (``merge_state_into_meta``,
``shared_from_meta``, перенос счётчиков напоминаний, копирование полей
сессии перед записью): поле ``SessionState`` задано ровно тогда, когда
прежний путь клал ключ в meta develop, — поэтому файлы треда на диске
остаются прежними.

Политика напоминаний (``message_policy``) ведёт свои счётчики в изменяемом
словаре — «notice»: у сессии это не meta треда, а только эти четыре ключа.
"""

from __future__ import annotations

from collections.abc import Mapping
from types import SimpleNamespace
from typing import Any

from gc_core.execution.state import SharedStateData
from gc_core.intent_log.serialize import deserialize_list, serialize_for_meta
from gc_core.message_policy.tool_usage_notice.state import (
    META_MEMORY_NOTICE_CNT,
    META_PROJECT_MEMORY_CHECK_CNT,
    META_PROJECT_MEMORY_CONTENT_HASH,
    META_TODO_REMINDER_CNT,
    META_TOOL_USAGE_NOTICE_CNT,
    hydrate_tool_usage_notice_state,
    persist_tool_usage_notice_state,
)
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    normalize_permission_mode,
    normalize_session_todo,
    normalize_working_memory,
    reject_legacy_memory_keys,
    serialize_shared_block,
    validate_thread_meta_for_load,
)
from gc_core.persistence.thread_meta_mapper import read_optional_nonneg_int
from gc_core.plan_store import normalize_plan_block
from gc_core.threads.meta_map import shared_fields
from gc_core.threads.models import JsonObject, SessionState, Usage

#: Ключ счётчика напоминаний → поле ``SessionState``.
_NOTICE_FIELDS = (
    (META_PROJECT_MEMORY_CONTENT_HASH, "project_memory_version"),
    (META_TOOL_USAGE_NOTICE_CNT, "tool_usage_notice"),
    (META_TODO_REMINDER_CNT, "todo_reminder_count"),
    (META_PROJECT_MEMORY_CHECK_CNT, "project_memory_check_count"),
)
_SCHEMA_KEY = "state_schema_version"
_REJECTED = "__rejected__"


def _unset(session: SessionState, names: list[str]) -> SessionState:
    """Поля без значения — как ключа нет в meta (значение по умолчанию)."""
    present = [n for n in names if n in session.model_fields_set]
    if not present:
        return session
    defaults = {
        name: SessionState.model_fields[name].get_default(
            call_default_factory=True
        )
        for name in present
    }
    out = session.model_copy(update=defaults)
    for name in present:
        out.__pydantic_fields_set__.discard(name)
    return out


def _rejected_shared(extra: Mapping[str, Any]) -> dict[str, Any]:
    """Значения ``shared``, которые модель не приняла (лежат в ``extra``)."""
    out: dict[str, Any] = {}
    for path, value in extra.get(_REJECTED) or []:
        if len(path) == 2 and path[0] == "shared":
            out[path[1]] = value
    return out


def _shared_raw(
    session: SessionState, extra: Mapping[str, Any]
) -> dict[str, Any]:
    """Блок ``shared``, как он лежит в треде: принятые поля и отвергнутые."""
    raw = {
        key: getattr(session, field)
        for key, field in shared_fields().items()
        if field in session.model_fields_set
    }
    raw.update(_rejected_shared(extra))
    return raw


def validate_for_load(session: SessionState, extra: Mapping[str, Any]) -> None:
    """Прежняя проверка загрузки: устаревшие поля памяти — отказ; явно
    чужая версия схемы состояния — отказ. Неизвестные ключи записи идут в
    проверку как есть: в meta develop они лежали рядом с состоянием."""
    view: JsonObject = {
        **extra,
        "shared": _shared_raw(session, extra),
        "user_intent_log": session.intent_log,
        "current_user_intent": session.current_intent,
        _SCHEMA_KEY: extra.get(_SCHEMA_KEY, STATE_SCHEMA_VERSION),
    }
    validate_thread_meta_for_load(view)


def shared_from_session(
    session: SessionState, extra: Mapping[str, Any] | None = None
) -> SharedStateData | None:
    """Общее состояние агента треда; блока ``shared`` нет — ``None``."""
    extra = extra or {}
    validate_for_load(session, extra)
    raw = _shared_raw(session, extra)
    if not raw:
        return None
    return SharedStateData(
        working_memory=normalize_working_memory(raw.get("working_memory")),
        notes_md=str(raw.get("notes_md") or ""),
        fetch_url_cache=dict(raw.get("fetch_url_cache") or {}),
        confirm_rules=dict(raw.get("confirm_rules") or {}),
        session_todo=normalize_session_todo(raw.get("session_todo")),
        permission_mode=normalize_permission_mode(raw.get("permission_mode")),
        plan=normalize_plan_block(raw.get("plan")),
        mode_announced=normalize_permission_mode(raw.get("mode_announced")),
        plan_removed_notice=bool(raw.get("plan_removed_notice", False)),
        judge_denials_in_a_row=read_optional_nonneg_int(
            raw.get("judge_denials_in_a_row")
        )
        or 0,
        judge_denials_total=read_optional_nonneg_int(
            raw.get("judge_denials_total")
        )
        or 0,
        judge_paused=raw.get("judge_paused") is True,
    )


def notice_state(
    session: SessionState, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """Счётчики напоминаний треда — изменяемый словарь политики."""
    extra = extra or {}
    out = {
        key: getattr(session, field)
        for key, field in _NOTICE_FIELDS
        if field in session.model_fields_set
    }
    if META_MEMORY_NOTICE_CNT in extra:
        out[META_MEMORY_NOTICE_CNT] = extra[META_MEMORY_NOTICE_CNT]
    return out


def _usage_meta(usage: Usage) -> dict[str, Any] | None:
    if not usage.model_fields_set and not usage.model_extra:
        return None
    return usage.model_dump(mode="json", exclude_unset=True)


def hydrate_state(
    state: Any,
    session: SessionState,
    usage: Usage,
    notice: Mapping[str, Any],
) -> None:
    """Поля ``State`` хода из сохранённого состояния треда."""
    from gc_core.usage.account import UsageAccount

    state.init_user_intent_log(session.intent_log)
    state.init_current_user_intent(session.current_intent)
    state.init_user_turn_seq(session.user_turn_seq)
    state.infer_user_turn_seq_if_missing()
    hydrate_tool_usage_notice_state(state, dict(notice))
    state.dialog_steps_offset = int(session.dialog_steps_offset or 0)
    state.inbox_seq = int(session.inbox_seq or 0)
    state.last_main_prompt_tokens = read_optional_nonneg_int(
        session.last_prompt_tokens
    )
    state.last_main_prompt_message_count = read_optional_nonneg_int(
        session.last_prompt_message_count
    )
    state.usage = UsageAccount.from_meta(_usage_meta(usage))


def _shared_values(state: Any) -> tuple[dict[str, Any], list[str]]:
    """Поля блока ``shared`` хода: заданные и снятые (блок пишется целиком)."""
    shared = getattr(state, "shared", None)
    if not isinstance(shared, SharedStateData):
        return {}, []
    block = serialize_shared_block(shared)
    reject_legacy_memory_keys({"shared": block})
    values: dict[str, Any] = {}
    unset: list[str] = []
    for key, field in shared_fields().items():
        if key in block:
            values[field] = block[key]
        else:
            unset.append(field)
    return values, unset


def session_with_shared(base: SessionState, shared: Any) -> SessionState:
    """Состояние треда с блоком ``shared`` хода (блок пишется целиком)."""
    values, unset = _shared_values(SimpleNamespace(shared=shared))
    return _unset(base.model_copy(update=values), unset)


def _prompt_values(
    base: SessionState, rendered: str | None, backend_api_mode: str
) -> tuple[dict[str, Any], list[str]]:
    """Отрендеренный system prompt: в chat/completions — новый, если есть
    (иначе прежний); в responses его в истории нет — снимается."""
    del base
    if backend_api_mode != "chat_completions":
        return {}, ["rendered_system_prompt"]
    if isinstance(rendered, str) and rendered.strip():
        return {"rendered_system_prompt": rendered}, []
    return {}, []


#: Приостановка хода на вопросе: вопрос, ждущий ответа, и снимок хода.
_SUSPENSION_FIELDS = ("pending_question", "suspended_turn")


def _suspension_values(base: SessionState, state: Any) -> dict[str, Any]:
    """Ход приостановку либо задал (вопрос), либо снял (ответ, продолжение)
    — прежняя снимается первой же записью хода."""
    values: dict[str, Any] = {}
    for name in _SUSPENSION_FIELDS:
        value = getattr(state, name, None)
        if value or getattr(base, name) is not None:
            values[name] = dict(value) if value else None
    return values


def session_from_state(
    base: SessionState,
    state: Any,
    *,
    notice: dict[str, Any],
    rendered_system_prompt: str | None,
    backend_api_mode: str,
    previous_response_id: str | None,
) -> SessionState:
    """Состояние треда после шага хода: ``base`` — сохранённое, поверх —
    то, что ход меняет. ``notice`` пополняется счётчиками хода (как meta
    сессии в прежнем пути)."""
    values, unset = _shared_values(state)
    prompt, prompt_unset = _prompt_values(
        base, rendered_system_prompt, backend_api_mode
    )
    values.update(prompt)
    unset += prompt_unset
    field = getattr
    values.update(
        intent_log=serialize_for_meta(
            field(state, "user_intent_log", []) or []
        ),
        current_intent=serialize_for_meta(
            field(state, "current_user_intent", []) or []
        ),
        user_turn_seq=int(field(state, "user_turn_seq", 0) or 0),
        dialog_steps_offset=int(field(state, "dialog_steps_offset", 0) or 0),
        last_prompt_tokens=read_optional_nonneg_int(
            field(state, "last_main_prompt_tokens", None)
        ),
        last_prompt_message_count=read_optional_nonneg_int(
            field(state, "last_main_prompt_message_count", None)
        ),
        previous_response_id=previous_response_id,
    )
    inbox_seq = int(field(state, "inbox_seq", 0) or 0)
    if inbox_seq or "inbox_seq" in base.model_fields_set:
        values["inbox_seq"] = inbox_seq
    values.update(_suspension_values(base, state))
    last_backend = getattr(state, "last_backend_response_id", None)
    if isinstance(last_backend, str) and last_backend:
        values["last_backend_response_id"] = last_backend
    persist_tool_usage_notice_state(state, notice)
    for key, field in _NOTICE_FIELDS:
        if key in notice:
            values[field] = notice[key]
    return _unset(base.model_copy(update=values), unset)


def _reserialized(records: list[Any]) -> list[Any]:
    return serialize_for_meta(deserialize_list(records))


def _compaction_shared(
    payload: Mapping[str, Any],
) -> tuple[dict[str, Any], list[str]]:
    """Блок ``shared`` из сводки — целиком вместо прежнего."""
    shared = payload.get("shared")
    if not isinstance(shared, dict):
        return {}, []
    values: dict[str, Any] = {}
    unset: list[str] = []
    for key, field in shared_fields().items():
        if key in shared:
            values[field] = shared[key]
        else:
            unset.append(field)
    return values, unset


def _compaction_intent(
    base: SessionState, payload: Mapping[str, Any]
) -> dict[str, Any]:
    """Лог намерений: из сводки, иначе прежний; номер хода — так же."""
    values: dict[str, Any] = {}
    fields_set = base.model_fields_set
    log = payload.get("user_intent_log")
    if isinstance(log, list):
        values["intent_log"] = _reserialized(log)
    elif "intent_log" in fields_set:
        values["intent_log"] = _reserialized(base.intent_log)
    if "current_intent" in fields_set:
        values["current_intent"] = _reserialized(base.current_intent)
    seq = payload.get("user_turn_seq")
    if seq is not None:
        values["user_turn_seq"] = int(seq or 0)
    elif "user_turn_seq" in fields_set:
        values["user_turn_seq"] = int(base.user_turn_seq or 0)
    return values


def session_after_compaction(
    base: SessionState, summary_payload: Mapping[str, Any], *, now_iso: str
) -> SessionState:
    """Состояние треда после сжатия истории сводкой."""
    values, unset = _compaction_shared(summary_payload)
    try:
        offset = int(summary_payload.get("dialog_steps_offset", 0))
    except (TypeError, ValueError):
        offset = int(base.dialog_steps_offset or 0)
    values.update(
        previous_response_id=None,
        last_summarized_at=now_iso,
        dialog_steps_offset=max(0, offset),
        last_prompt_tokens=read_optional_nonneg_int(
            summary_payload.get("last_main_prompt_tokens")
        ),
        last_prompt_message_count=read_optional_nonneg_int(
            summary_payload.get("last_main_prompt_message_count")
        ),
    )
    values.update(_compaction_intent(base, summary_payload))
    return _unset(base.model_copy(update=values), unset)


def usage_from_state(state: Any) -> Usage:
    """Расход токенов треда — кумулятив хода."""
    return Usage.model_validate(state.usage.to_meta())


def extra_for_write(extra: Mapping[str, Any]) -> dict[str, Any]:
    """``extra`` записи: версия схемы состояния есть всегда (как в meta
    develop, которую видел прежний путь)."""
    out = dict(extra)
    out.setdefault(_SCHEMA_KEY, STATE_SCHEMA_VERSION)
    return out
