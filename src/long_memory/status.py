"""Project Info string builder for long memory.

Pure function — no side effects, no knowledge of ctx/state.
Intentionally never reveals the file path, the tool name, or any internal mechanics
to the model. The agent learns *that* a memory exists and *roughly how big it is*;
*how to access it* is conveyed via the tool registry, not via this string.
"""
from __future__ import annotations

from pathlib import Path

from long_memory import storage

_EMPTY_STATUS = (
    "project doesn't have long memory. "
    "Use appropriate tool to store important facts for future conversations."
)


def build_long_memory_status(path: Path) -> str:
    """Return the one-line status injected into Project Info as longMemoryStatus.

    Two outputs by file state:
      - missing/empty/whitespace-only → empty-status copy nudging the model to seed.
      - populated → "project has long memory — N chars, L lines. Read it with
        appropriate tool."
    """
    info = storage.stat(path)
    if not info.exists or info.chars == 0:
        return _EMPTY_STATUS
    text = storage.read(path)
    if not text.strip():
        return _EMPTY_STATUS
    return (
        f"project has long memory — {info.chars} chars, {info.lines} lines. "
        f"Read it with appropriate tool."
    )
