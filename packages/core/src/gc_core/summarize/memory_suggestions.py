"""Transport-only LLM calls for working_memory patch steps after summary."""

from dataclasses import dataclass
from typing import Any, Dict, List

from gc_core.agent_store import agent_store_of
from gc_core.execution.resources import (
    PromptResourceRequest,
    aload_prompt_resource,
)
from gc_core.summarize.llm import arequest_summary_text
from gc_core.summarize.prompt_loading import aload_memory_patch_prompt


@dataclass
class MemoryPatchLlmResult:
    """LLM transport result for a memory patch step (no parsing here).

    ``raw_response`` is already ``for_model``-projected (aux choke in
    ``request_summary_text``); dirty text is logged there, not here.
    """

    request_messages: List[Dict[str, str]]
    raw_response: str


async def _arequest_patch(
    *,
    ctx: Any,
    state: Any,
    system_prompt: str,
    user_prompt: str,
    request_label: str,
) -> MemoryPatchLlmResult:
    """Транспорт memory-patch шага: собрать messages и сходить в aux-LLM.

    Реализация одна и она здесь; ``_request_patch`` — синхронный шим над ней.
    """
    logger = getattr(ctx, "logger", None)
    request_messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_prompt},
    ]
    # The draft parser needs the original indentation to identify code boundaries.
    projected = await arequest_summary_text(
        ctx=ctx,
        state=state,
        messages=request_messages,
        request_label=request_label,
        consumer="summary",
        logger=logger,
        strip_outer_whitespace=False,
    )
    return MemoryPatchLlmResult(
        request_messages=request_messages,
        raw_response=projected,
    )


def _is_subagent_state(state: Any) -> bool:
    """Child agent State has parent_state_id; main orchestrator does not."""
    return getattr(state, "parent_state_id", None) is not None


async def arequest_patch_replace(
    *,
    ctx: Any,
    state: Any,
    history_text: str,
    summary_text: str,
    persistent_state_block: str = "",
) -> MemoryPatchLlmResult:
    """LLM step: full replace snapshots for all working_memory registers.

    Основная реализация шага; ``request_patch_replace`` — синхронный шим.
    """
    subagent = _is_subagent_state(state)
    # Soft-only: subagent addendum lives in system prompt only (no user duplicate).
    system = await aload_prompt_resource(
        ctx,
        PromptResourceRequest("memory_patch", subagent=subagent),
        local=lambda: aload_memory_patch_prompt(
            agent_store_of(ctx), subagent=subagent
        ),
    )
    user_prompt = (
        "<dialog_history>\n"
        f"{history_text}\n"
        "</dialog_history>\n\n"
        "<current_summary>\n"
        f"{summary_text}\n"
        "</current_summary>\n\n"
        f"{str(persistent_state_block or '').strip()}\n"
    )
    return await _arequest_patch(
        ctx=ctx,
        state=state,
        system_prompt=system,
        user_prompt=user_prompt,
        request_label="working_memory_patch_replace_after_summary",
    )
