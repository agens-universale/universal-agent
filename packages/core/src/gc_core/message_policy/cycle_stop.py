from __future__ import annotations

from typing import Any, Callable, Dict, List, Optional

from gc_core.tools import aapply_tool
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_core.tools.session.stored_outputs.wrapping import maybe_store_and_wrap_tool_output
from gc_core.utils.generate_message_id import generate_msg_id_sha256 as generate_message_id_func


async def _maybe_await(value):
    """Шов подмены терпит обе формы заглушки — см. processing_responses._maybe_await."""
    import inspect

    if inspect.isawaitable(value):
        return await value
    return value



async def _resolve_finish_turn_output_on_cycle_stop(
    *,
    state: Any,
    ctx: Any,
    tool_arguments: str,
    run_agent_func: Callable,
) -> str:
    """Build finish-turn tool output when the model stopped mid pending tool_calls."""
    candidate = SubmitSolution.output_from_cycle_stop_arguments(tool_arguments)
    if isinstance(candidate, str) and candidate.strip():
        return candidate
    try:
        applied = await _maybe_await(aapply_tool(
            tool_name=FINISH_TOOL_NAME,
            arguments=tool_arguments,
            ctx=ctx,
            state=state,
            run_agent_func=run_agent_func,
        ))
        if isinstance(applied, str) and applied.strip():
            return applied
    except Exception:
        pass
    return SubmitSolution.cycle_stop_output_error(
        detail="Could not build finish-turn output from function_call.arguments.",
    )


async def _resolve_tool_output_on_cycle_stop(
    *,
    state: Any,
    ctx: Any,
    tool_name: str,
    tool_arguments: str,
    run_agent_func: Callable,
) -> str:
    if tool_name == FINISH_TOOL_NAME:
        return await _resolve_finish_turn_output_on_cycle_stop(
            state=state,
            ctx=ctx,
            tool_arguments=tool_arguments,
            run_agent_func=run_agent_func,
        )

    tool_res = None
    tools_data = getattr(ctx, "tools_data", None)
    if isinstance(tools_data, dict):
        tool_data = tools_data.get(tool_name, {})
        tool_fabric = tool_data.get("tool_fabric") if isinstance(tool_data, dict) else None
        if callable(tool_fabric):
            try:
                tool = tool_fabric(
                    ctx=ctx,
                    state=state,
                    config_dict=getattr(ctx, "config_dict", {}) or {},
                    run_agent_func=run_agent_func,
                )
                cycle_stop_output = getattr(tool, "tool_output_on_cycle_stop", None)
                if callable(cycle_stop_output):
                    candidate = cycle_stop_output()
                    if isinstance(candidate, str) and candidate.strip():
                        tool_res = candidate
            except Exception:
                tool_res = None
    if not isinstance(tool_res, str) or not tool_res.strip():
        tool_res = AbstractSimpleTool.fallback_tool_output_on_cycle_stop(tool_name)
    return tool_res


async def aappend_cycle_stop_tool_outputs_for_batch(
    *,
    state: Any,
    ctx: Any,
    tool_calls: List[Dict],
    run_agent_func: Callable,
    persist_messages_callback: Optional[Callable[[List[Dict]], None]],
    backend_mode: str,
) -> None:
    if backend_mode != "chat_completions":
        return
    synthetic_messages: List[Dict] = []
    # I3: finish output персистится один раз уже после apply_finish_gate — не здесь.
    immediate_persist: List[Dict] = []
    for tool_call in tool_calls:
        call_id = tool_call.get("call_id")
        tool_name = str(tool_call.get("name") or "")
        if not isinstance(call_id, str) or not call_id:
            continue
        tool_res = await _resolve_tool_output_on_cycle_stop(
            state=state,
            ctx=ctx,
            tool_name=tool_name,
            tool_arguments=str(tool_call.get("arguments", "")),
            run_agent_func=run_agent_func,
        )
        tool_res = maybe_store_and_wrap_tool_output(
            tool_res,
            call_id=call_id,
            tool_name=tool_name,
            state=state,
            config=getattr(ctx, "config_dict", {}) or {},
            logger=getattr(ctx, "logger", None),
        )
        msg = {
            "id": generate_message_id_func("fco"),
            "type": "function_call_output",
            "call_id": call_id,
            "output": tool_res,
        }
        synthetic_messages.append(msg)
        if tool_name != FINISH_TOOL_NAME:
            immediate_persist.append(msg)
    if not synthetic_messages:
        return
    state.messages.extend(synthetic_messages)
    if persist_messages_callback is not None and immediate_persist:
        persist_messages_callback(immediate_persist)


def append_cycle_stop_tool_outputs_for_batch(*args, **kwargs):
    """Синхронный двойник — для вызывающих вне цикла агента и тестов."""
    from gc_core.utils.portal import get_portal

    return get_portal().run(aappend_cycle_stop_tool_outputs_for_batch(*args, **kwargs))
