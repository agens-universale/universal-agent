"""Apply explicit operation results to the state owned by the agent loop."""

from collections import OrderedDict
from copy import deepcopy
from typing import Any

from gc_core.execution.contracts import (
    ModelResult,
    StateDelta,
    ToolStateView,
    ToolVisionView,
)
from gc_core.usage.account import UsageAccount


def tool_vision_view(state: Any) -> ToolVisionView:
    """Срез State для ``fs_view_image`` — без файлов и сети (код workflow)."""
    from gc_core.vision.audience import view_tool_in_set

    known = [
        deepcopy(item)
        for message in getattr(state, "messages", None) or []
        if isinstance(message, dict)
        for item in message.get("attachments") or []
        if isinstance(item, dict)
    ]
    return ToolVisionView(
        probe_turn=getattr(state, "vision_probe_turn", None),
        turn_level=getattr(state, "turn_level", None),
        live_ids=sorted(getattr(state, "vision_live_ids", None) or ()),
        step_attachments=deepcopy(
            list(getattr(state, "pending_tool_attachments", None) or [])
        ),
        known_attachments=known,
        view_tool_in_set=view_tool_in_set(getattr(state, "tools", None) or []),
        state_id=getattr(state, "state_id", None),
        x_target=getattr(state, "x_target", None),
    )


def tool_state_view(
    state: Any, *, vision: bool = False, fetch_cache: bool = True
) -> ToolStateView:
    """Срез состояния для инструмента. ``fetch_cache`` — кэш страниц
    ``fetch_url`` (до 10 страниц текста): остальным инструментам он не нужен
    и только раздувает каждый запрос эффекта."""
    shared = getattr(state, "shared", None)
    return ToolStateView(
        vision=tool_vision_view(state) if vision else None,
        working_memory=deepcopy(getattr(shared, "working_memory", {}) or {}),
        session_todo=deepcopy(getattr(state, "session_todo", []) or []),
        fetch_url_cache=[
            [key, deepcopy(value)]
            for key, value in (
                getattr(shared, "fetch_url_cache", {}) or {}
            ).items()
        ]
        if fetch_cache
        else [],
    )


def apply_state_delta(state: Any, delta: StateDelta) -> None:
    if delta.schema_version != 1:
        raise ValueError("Unsupported StateDelta schema_version")
    if delta.working_memory is not None:
        state.shared.working_memory = deepcopy(delta.working_memory)
    if delta.fetch_url_cache is not None:
        state.shared.fetch_url_cache = OrderedDict(
            deepcopy(delta.fetch_url_cache)
        )
    if delta.session_todo is not None:
        # State property preserves the separate parent/child todo scopes.
        state.session_todo = deepcopy(delta.session_todo)
    if delta.pending_tool_attachments:
        pending = getattr(state, "pending_tool_attachments", None)
        if pending is None:
            pending = state.pending_tool_attachments = []
        pending.extend(deepcopy(delta.pending_tool_attachments))


def apply_model_result(state: Any, result: ModelResult, *, main: bool) -> None:
    if result.schema_version != 1:
        raise ValueError("Unsupported ModelResult schema_version")
    state.usage.merge(UsageAccount.from_meta(result.usage))
    if result.vision is not None:
        # Как при живом State в develop: что ушло модели целиком и потолок
        # картинок хода живут в состоянии до следующего запроса.
        from gc_core.vision.rejects import image_limit_from_data

        state.vision_live_ids = frozenset(result.vision.get("live_ids") or ())
        state.vision_request_limit = image_limit_from_data(
            result.vision.get("request_limit")
        )
    if main:
        state.last_finish_reason = result.finish_reason
        state.last_main_prompt_tokens = result.prompt_tokens
        state.last_main_prompt_message_count = result.prompt_message_count
