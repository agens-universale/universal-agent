"""Explicit resource boundaries shared by local and durable execution."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any, Literal

from gc_core.execution.ports import port


@dataclass(frozen=True)
class PromptResourceRequest:
    kind: Literal[
        "summary",
        "memory_patch",
        "limit_summary",
        "intent_tool",
        "intent_final",
        "project_memory",
        "finish_gate",
    ]
    path: str | None = None
    subagent: bool = False
    schema_version: int = 1


PromptResourceLoader = Callable[[PromptResourceRequest], Awaitable[str]]


async def aload_prompt_resource(
    ctx: Any,
    request: PromptResourceRequest,
    *,
    local: Callable[[], str],
) -> str:
    """Only the local branch may resolve paths or read prompt files.

    Transport errors must propagate; falling back to local I/O would break replay.
    The callable stays in the process and is never part of the request payload.
    """
    loader = port(ctx, "prompt_resource")
    if loader is not None:
        return await loader(request)
    return local()
