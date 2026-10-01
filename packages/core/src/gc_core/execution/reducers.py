"""Apply explicit operation results to the state owned by the agent loop."""

from collections import OrderedDict
from copy import deepcopy
from typing import Any

from gc_core.execution.contracts import ModelResult, StateDelta, ToolStateView
from gc_core.usage.account import UsageAccount


def tool_state_view(state: Any) -> ToolStateView:
    shared = getattr(state, "shared", None)
    return ToolStateView(
        working_memory=deepcopy(getattr(shared, "working_memory", {}) or {}),
        session_todo=deepcopy(getattr(state, "session_todo", []) or []),
        fetch_url_cache=[
            [key, deepcopy(value)]
            for key, value in (
                getattr(shared, "fetch_url_cache", {}) or {}
            ).items()
        ],
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


def apply_model_result(state: Any, result: ModelResult, *, main: bool) -> None:
    if result.schema_version != 1:
        raise ValueError("Unsupported ModelResult schema_version")
    state.usage.merge(UsageAccount.from_meta(result.usage))
    if main:
        state.last_finish_reason = result.finish_reason
        state.last_main_prompt_tokens = result.prompt_tokens
        state.last_main_prompt_message_count = result.prompt_message_count
