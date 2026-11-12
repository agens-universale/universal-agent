"""Worker/local prompt readers. Imported only when an external read executes."""

from gc_core.agent_store import agent_store_of
from gc_core.execution.resources import PromptResourceRequest


async def aload_prompt_resource_here(
    request: PromptResourceRequest, *, ctx
) -> str:
    if request.schema_version != 1:
        raise ValueError("Unsupported prompt resource schema_version")
    readers = {
        "summary": _summary,
        "memory_patch": _memory_patch,
        "limit_summary": _limit,
        "intent_tool": _intent_tool,
        "intent_final": _intent_final,
        "project_memory": _project_memory,
        "finish_gate": _finish_gate,
        "routing_pair_judge": _routing_pair_judge,
    }
    try:
        reader = readers[request.kind]
    except KeyError:
        raise ValueError("Unknown prompt resource kind") from None
    return await reader(request, ctx)


async def _summary(_request, ctx):
    from gc_core.summarize.prompt_loading import aload_summary_prompt

    return await aload_summary_prompt(agent_store_of(ctx))


async def _memory_patch(request, ctx):
    from gc_core.summarize.prompt_loading import aload_memory_patch_prompt

    return await aload_memory_patch_prompt(
        agent_store_of(ctx), subagent=request.subagent
    )


async def _limit(_request, ctx):
    from gc_core.summarize.limit_trace_summary import (
        aload_limit_summary_prompt,
    )

    return await aload_limit_summary_prompt(agent_store_of(ctx))


async def _intent_tool(_request, ctx):
    from gc_core.intent_scope.summarizer import aload_intent_prompt

    return await aload_intent_prompt(agent_store_of(ctx), "intent_tool")


async def _intent_final(_request, ctx):
    from gc_core.intent_scope.summarizer import aload_intent_prompt

    return await aload_intent_prompt(agent_store_of(ctx), "intent_final")


async def _project_memory(_request, ctx):
    from gc_core.project_memory.compaction import aload_compaction_prompt

    return await aload_compaction_prompt(agent_store_of(ctx))


async def _finish_gate(_request, ctx):
    from gc_core.intent_guard.final_report_consistency import (
        aload_guard_prompt,
    )

    return await aload_guard_prompt(agent_store_of(ctx))


async def _routing_pair_judge(_request, ctx):
    from gc_core.auto_routing.judge_runner import aload_judge_prompt

    return await aload_judge_prompt(agent_store_of(ctx))
