"""Круговой путь «состояние агента → meta треда на диске → состояние» для
тестов сохранения: тем же переводом (``gc_core.persistence.session_state``)
и той же раскладкой в meta develop (``meta_map``), что у сессии с файловым
хранилищем."""

from __future__ import annotations

from typing import Any

from gc_core.execution.state import SharedStateData
from gc_core.persistence import session_state as conv
from gc_core.persistence.thread_meta_mapper import read_optional_nonneg_int
from gc_core.threads.meta_map import meta_from_parts, parts_from_meta


def shared_from_meta(meta: dict) -> SharedStateData | None:
    """Блок ``shared`` треда, как его поднимает ход."""
    parts = parts_from_meta(dict(meta or {}))
    return conv.shared_from_session(parts.state, parts.extra)


def _meta(meta: dict, parts) -> dict:
    out = meta_from_parts(parts, base=meta)
    out.setdefault("state_schema_version", "v2")
    return out


def merge_shared_into_meta(meta: dict, shared: Any) -> dict:
    """Meta треда после записи блока ``shared`` хода."""
    parts = parts_from_meta(dict(meta or {}))
    state = conv.session_with_shared(parts.state, shared)
    return _meta(meta, parts.with_(state=state))


def merge_state_into_meta(
    meta: dict,
    state: Any,
    *,
    backend_api_mode: str = "chat_completions",
    rendered_system_prompt: str | None = None,
) -> dict:
    """Meta треда после записи шага хода с состоянием ``state``."""
    parts = parts_from_meta(dict(meta or {}))
    notice = conv.notice_state(parts.state, parts.extra)
    session = conv.session_from_state(
        parts.state,
        state,
        notice=notice,
        rendered_system_prompt=rendered_system_prompt,
        backend_api_mode=backend_api_mode,
        previous_response_id=getattr(state, "previous_response_id", None),
    )
    usage = parts.usage
    if getattr(state, "usage", None) is not None:
        usage = conv.usage_from_state(state)
    return _meta(meta, parts.with_(state=session, usage=usage))


def read_prompt_token_fields(meta: dict) -> tuple[int | None, int | None]:
    """Счётчики промпта последнего запроса к модели из meta треда."""
    return (
        read_optional_nonneg_int(meta.get("last_main_prompt_tokens")),
        read_optional_nonneg_int(meta.get("last_main_prompt_message_count")),
    )
