"""Worker/local prompt readers. Imported only when an external read executes."""

from gc_core.execution.resources import PromptResourceRequest


def load_prompt_resource(request: PromptResourceRequest, *, ctx) -> str:
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
    }
    try:
        reader = readers[request.kind]
    except KeyError:
        raise ValueError("Unknown prompt resource kind") from None
    return reader(request, ctx)


def _summary(request, _ctx):
    from gc_core.summarize import prompt_loading

    return prompt_loading.load_prompt_template(
        str(request.path or prompt_loading.default_prompt_path())
    )


def _memory_patch(request, ctx):
    from gc_core.summarize.memory_suggestions import (
        _resolve_prompt_path,
        default_memory_patch_path,
    )
    from gc_core.summarize.prompt_loading import (
        load_memory_patch_replace_prompt,
    )

    path = _resolve_prompt_path(
        ctx,
        "chat_completions_memory_patch_replace_prompt_path",
        default_memory_patch_path(),
    )
    return load_memory_patch_replace_prompt(path, subagent=request.subagent)


def _limit(_request, _ctx):
    from gc_core.summarize.limit_trace_summary import (
        _load_limit_summary_prompt,
    )

    return _load_limit_summary_prompt()


def _intent_tool(_request, ctx):
    from gc_core.intent_scope.summarizer import _load_system_prompt

    return _load_system_prompt(ctx)


def _intent_final(_request, ctx):
    from gc_core.intent_scope.summarizer import (
        _load_prompt_file,
        _resolve_final_answer_prompt_path,
    )

    return _load_prompt_file(_resolve_final_answer_prompt_path(ctx))


def _project_memory(_request, _ctx):
    from gc_core.project_memory.compaction import _load_prompt

    return _load_prompt()


def _finish_gate(request, _ctx):
    from gc_core.intent_guard.final_report_consistency import (
        _load_guard_prompt,
    )

    return _load_guard_prompt(request.path)
