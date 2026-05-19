"""Project Info string builder for long memory.

Pure function — no side effects, no knowledge of ctx/state.
Intentionally never reveals the file path, the tool name, or any internal mechanics
to the model. The agent learns *that* a memory exists and *roughly how big it is*;
*how to access it* is conveyed via the tool registry, not via this string.
"""
from __future__ import annotations

from pathlib import Path
from typing import Optional

from long_memory import storage

_FILE_NAME = "long_memory.md"

_EMPTY_STATUS = (
    "project doesn't have long memory yet. "
    "When you discover or verify a durable fact (project convention, naming rule, "
    "architectural decision, user preference), use the appropriate memory tool "
    "with action=append/write to persist it for future sessions."
)


def build_long_memory_status_for_cwd(cwd: Optional[str] = None) -> str:
    """Resolve the project-local memory path and build the status string.

    Callers that have no business knowing where memory lives use this entry
    point — internal file name and directory layout stay encapsulated.
    """
    from config_modules.env_vars import project_gc_dir

    return build_long_memory_status(project_gc_dir(cwd) / _FILE_NAME)


def attach_status_to_prompt_vars(
    prompt_vars: dict, cwd: Optional[str] = None
) -> None:
    """Add `longMemoryStatus` to the given prompt-variables dict.

    Sole entry point for integrating long memory into the system prompt;
    keeps the prompt-variable key name, the file name, the directory
    layout and any rendering policy inside this module so that callers
    (State / AgentExecutor / tests) stay decoupled from internals.

    Safe by contract: any failure is swallowed so memory issues never
    break system-prompt assembly.
    """
    try:
        prompt_vars["longMemoryStatus"] = build_long_memory_status_for_cwd(cwd)
    except Exception:  # pragma: no cover — memory must never break metadata
        return


def build_long_memory_status(path: Path) -> str:
    """Return the multi-line status injected into Project Info as longMemoryStatus.

    Two outputs by file state:
      - missing/empty/whitespace-only → empty-status nudging the model to persist
        durable facts when encountered.
      - populated → imperative "MUST call read AS YOUR FIRST STEP" — model is not
        expected to interpret it as optional; the imperative is reinforced by the
        tool description and the system prompt rule (Long memory protocol).
    """
    info = storage.stat(path)
    if not info.exists or info.chars == 0:
        return _EMPTY_STATUS
    text = storage.read(path)
    if not text.strip():
        return _EMPTY_STATUS
    return (
        f"project has long memory — {info.chars} chars, {info.lines} lines.\n"
        "You MUST call the appropriate memory tool with action=read AS YOUR FIRST "
        "STEP of this session, before reading any project file or producing/"
        "modifying code.\n"
        "Skip ONLY if the task is unambiguously unrelated to code, conventions, "
        "or project decisions (e.g. pure arithmetic, general questions)."
    )
