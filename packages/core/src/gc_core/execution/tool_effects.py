"""Native leaf effects after the shared preparation and security guard flows."""

from __future__ import annotations

import hashlib
import json
from collections import OrderedDict
from copy import deepcopy
from types import SimpleNamespace
from typing import Any

from gc_core.execution.contracts import (
    GuardPreconditionChanged,
    ToolEffectRequest,
    ToolResult,
)
from gc_core.security.dual_wrapper import arun_guard_chain, guard_wrapper
from gc_core.security.guard_probe import (
    active_guard_checks,
    evaluate_guard_probe,
)


def arguments_digest(arguments: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(
            arguments,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        ).encode()
    ).hexdigest()


# These tools own orchestration or pure state. Their native bodies stay in core.
# All other registered tools retain their native factories and leaf bodies.
_CORE_TOOLS = frozenset(
    {
        "session_memory",
        "session_todo",
        "session_compact",
        "session_think",
        "session_read_user_intent_log",
        "session_read_stored_tool_output",
        "project_memory",
        "turn_ask_human",
        "turn_finish",
        "turn_intermediate_reply",
        "exec_calculator",
        "exec_sleep",
    }
)


def uses_leaf_effect(name: str, tool: Any) -> bool:
    from gc_core.tools import _SubagentTool

    return name not in _CORE_TOOLS and not isinstance(tool, _SubagentTool)


async def invoke_native_tool(
    tool: Any,
    name: str,
    arguments: dict[str, Any],
    *,
    ctx: Any,
    state: Any,
    call_id: str | None,
    operation_id: str,
) -> str:
    from gc_core.execution.reducers import tool_state_view

    checks = []
    token = active_guard_checks.set(checks)

    async def send_effect(effect_data=None):
        request = ToolEffectRequest(
            operation_id=operation_id,
            name=name,
            call_id=call_id,
            arguments=deepcopy(arguments),
            arguments_digest=arguments_digest(arguments),
            state=tool_state_view(state),
            guard_checks=list(checks),
            effect_step="download" if effect_data is not None else "body",
            effect_data=effect_data or {},
        )
        result = await ctx.tool_effect(request)
        if result.schema_version != 1:
            raise ValueError("Unsupported tool effect result")
        from gc_core.execution.reducers import apply_state_delta

        if state is not None:
            apply_state_delta(state, result.delta)
        return result.output

    async def leaf():
        prepare_download = getattr(tool, "_prepare_download", None)
        if callable(prepare_download):
            return await prepare_download(
                **arguments, native_effect=send_effect
            )
        return await send_effect()

    try:
        return await arun_guard_chain(
            type(tool).acall,
            tool,
            invoke_leaf=leaf,
            **arguments,
        )
    finally:
        active_guard_checks.reset(token)


async def execute_native_tool_effect(
    request: ToolEffectRequest, *, ctx: Any
) -> ToolResult:
    """Worker handler: revalidate facts, then invoke the registered leaf body."""
    from gc_core.tools import _prepare_tool_call, _tool_result_to_text

    if request.schema_version != 1:
        raise ValueError("Unsupported tool effect request")
    if arguments_digest(request.arguments) != request.arguments_digest:
        raise GuardPreconditionChanged(
            "Tool arguments changed after guard evaluation"
        )
    _revalidate_guard_checks(request, ctx.config_dict)
    state = SimpleNamespace(
        shared=SimpleNamespace(
            working_memory=deepcopy(request.state.working_memory),
            fetch_url_cache=OrderedDict(
                deepcopy(request.state.fetch_url_cache)
            ),
        ),
        session_todo=deepcopy(request.state.session_todo),
    )
    prepared = _prepare_tool_call(
        request.name,
        request.arguments,
        ctx=ctx,
        state=state,
        call_id=request.call_id,
    )
    if isinstance(prepared, str):
        raise GuardPreconditionChanged(
            "Tool preparation changed after approval"
        )
    tool, arguments, dropped = prepared
    if dropped or arguments_digest(arguments) != request.arguments_digest:
        raise GuardPreconditionChanged("Tool schema changed after approval")
    if request.effect_step == "download":
        output = await _execute_download_effect(tool, arguments, request)
    elif request.effect_step == "body":
        if callable(getattr(tool, "_prepare_download", None)):
            raise GuardPreconditionChanged(
                "Download requires its prepared effect boundary"
            )
        func = type(tool).acall
        while (descriptor := guard_wrapper(func)) is not None:
            func = descriptor.inner
        output = _tool_result_to_text(await func(tool, **arguments))
    else:
        raise ValueError("Unknown tool effect step")
    from gc_core.tools.subagents.progress import parse_tool_result_status

    ok, _ = parse_tool_result_status(output)
    from gc_core.execution.contracts import StateDelta

    cache = [
        [key, deepcopy(value)]
        for key, value in state.shared.fetch_url_cache.items()
    ]
    delta = StateDelta(
        fetch_url_cache=cache
        if cache != request.state.fetch_url_cache
        else None
    )
    return ToolResult(
        output=output,
        outcome="tool_error" if ok is False else "ok",
        delta=delta,
    )


def _revalidate_guard_checks(request, config_dict):
    for check in request.guard_checks:
        current = evaluate_guard_probe(check.request, config_dict=config_dict)
        if current.facts_digest != check.result.facts_digest:
            raise GuardPreconditionChanged(
                "Guard facts changed after approval"
            )


async def _execute_download_effect(tool, arguments, request):
    if not callable(getattr(tool, "_download_effect", None)):
        raise GuardPreconditionChanged(
            "Download effect belongs to another tool"
        )
    destination = next(
        (
            check.result
            for check in request.guard_checks
            if check.request.kind == "download_destination"
        ),
        None,
    )
    filename, extension = tool._validate_filename(arguments["filename"])
    if destination is None:
        raise GuardPreconditionChanged("Download destination was not probed")
    expected = dict(
        normalized_url=arguments["url"].strip(),
        validated_filename=filename,
        ext=extension,
        download_root=destination.data["download_root"],
        max_bytes=destination.data["max_bytes"],
        dest=destination.canonical_path,
    )
    if request.effect_data != expected:
        raise GuardPreconditionChanged(
            "Download destination changed after approval"
        )
    return await tool._download_effect(**expected)
