"""Long memory simple tool — controller layer.

The agent sees only three actions: read / append / write. Path, lock, atomicity,
and compaction are entirely under the hood. Errors carry concrete actionable
messages without leaking implementation details.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional

from long_memory import compaction, storage
from long_memory.compaction import SOFT_APPEND_INPUT_LIMIT_CHARS
from long_memory.storage import MAX_CHARS, LongMemoryError
from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool

_FILE_NAME = "long_memory.md"

_READ_EMPTY_HINT = (
    "long memory is empty. Use action=write to seed it with important facts "
    "you want to remember across sessions."
)
_AUTO_COMPACTION_HINT = (
    "Memory was compacted to fit the limit. "
    "Call action=read if you want to see what was kept."
)


class LongMemory(AbstractSimpleTool):
    tool_name = "long_memory"

    def __init__(self, ctx: Any = None, state: Any = None, **kwargs: Any) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state
        gc_dir: Optional[Path] = getattr(ctx, "project_gc_dir", None)
        self._path: Optional[Path] = (Path(gc_dir) / _FILE_NAME) if gc_dir else None

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "long_memory",
            "description": (
                "Persistent project-scoped markdown memory. "
                "Survives CLI restarts, process kills, machine reboots. "
                "Three actions: read (load full content), append (add a short "
                "delta), write (replace entirely; rare). "
                "Use for project conventions, decisions, constraints, naming "
                "rules — anything a future session must know."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "append", "write"],
                        "description": (
                            "read = return the current memory content; "
                            "append = add ONLY the new delta at end; "
                            "write = replace the memory entirely (rare)."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown text for append/write. "
                            "For append: ONLY the new delta. "
                            "For write: the FULL updated markdown. Parameter is ignored for read action."
                        ),
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return '''Tool name: long_memory
Tool description: Persistent project-scoped markdown memory. Survives CLI restarts.
The model reads it at the start of a new session if Project Info reports the project
has long memory; appends short factual deltas as it learns new things; rewrites in
full only when prior memory is wholesale wrong.

Mandatory workflow:
- START of a new session: if Project Info says the project has long memory,
  call long_memory action=read to load it.
- WHEN you discover/verify something durable for the project: call action=append
  with ONLY the new delta (a short factual paragraph or bullets — not chain-of-thought).
- USE action=write only to rewrite the memory from scratch (rare; e.g. previous
  memory turned out to be wrong).

What to put in:
- Verified facts, decisions, constraints, paths, identifiers, gotchas.
- Short. Bullet-style preferred.

What NOT to put in:
- Long brainstorming or step-by-step reasoning.
- Guesses presented as facts.
- Transient task state that is irrelevant to future sessions.

Input schema: {"type":"object","properties":{"action":{"type":"string","enum":["read","append","write"]},"content":{"type":"string"}},"required":["action"],"additionalProperties":false}

Return format (JSON string):
{
  "tool":"long_memory",
  "ok": true|false,
  "action": "read|append|write",
  "length": number,
  "lines": number,
  "error": null|{"type":"...","message":"..."},
  "content": "..." (only for read),
  "compaction": {"performed": bool, "before_chars"?: number, "after_chars"?: number},
  "hint": "..." (when relevant)
}
'''

    def _ok(
        self,
        *,
        action: str,
        length: int,
        lines: int,
        content: Optional[str] = None,
        compaction_block: Optional[Dict[str, Any]] = None,
        hint: Optional[str] = None,
    ) -> str:
        payload: Dict[str, Any] = {
            "tool": "long_memory",
            "ok": True,
            "action": action,
            "length": length,
            "lines": lines,
            "error": None,
        }
        if content is not None:
            payload["content"] = content
        payload["compaction"] = compaction_block or {"performed": False}
        if hint is not None:
            payload["hint"] = hint
        return json.dumps(payload, ensure_ascii=False)

    def _err(self, *, action: str, error_type: str, message: str) -> str:
        return json.dumps(
            {
                "tool": "long_memory",
                "ok": False,
                "action": action,
                "error": {"type": error_type, "message": message},
            },
            ensure_ascii=False,
        )

    def __call__(self, action: str, content: str = "", **kwargs: Any) -> str:
        if self._path is None:
            return self._err(
                action=action,
                error_type="not_configured",
                message="long_memory path is not available in this context.",
            )
        if action == "read":
            return self._handle_read()
        if action == "append":
            return self._handle_append(content)
        if action == "write":
            return self._handle_write(content)
        return self._err(
            action=action,
            error_type="invalid_action",
            message=f"unknown action: {action!r}. expected one of read|append|write.",
        )

    def _handle_read(self) -> str:
        info = storage.stat(self._path)
        text = storage.read(self._path)
        is_empty = (not info.exists) or (not text.strip())
        return self._ok(
            action="read",
            length=info.chars if info.exists else 0,
            lines=info.lines if info.exists else 0,
            content=text if not is_empty else "",
            hint=_READ_EMPTY_HINT if is_empty else None,
        )

    def _handle_append(self, content: str) -> str:
        delta = content if isinstance(content, str) else ""
        delta_stripped = delta.strip()
        if not delta_stripped:
            return self._err(
                action="append",
                error_type="empty_content",
                message="content must be non-empty for append.",
            )
        if len(delta_stripped) > SOFT_APPEND_INPUT_LIMIT_CHARS:
            return self._err(
                action="append",
                error_type="input_too_long",
                message=(
                    f"delta is {len(delta_stripped)} chars "
                    f"(> {SOFT_APPEND_INPUT_LIMIT_CHARS}). "
                    "Rewrite more concisely (store facts, not raw text) and retry."
                ),
            )
        existing = storage.read(self._path)
        existing_stripped = existing.strip()
        combined = (
            f"{existing_stripped}\n\n{delta_stripped}"
            if existing_stripped
            else delta_stripped
        )
        if len(combined) <= MAX_CHARS:
            try:
                info = storage.append(self._path, delta_stripped)
            except LongMemoryError as e:
                return self._err(action="append", error_type=e.code, message=e.message)
            return self._ok(action="append", length=info.chars, lines=info.lines)
        # Overflow path: compact existing + delta together; LLM decides what to keep.
        result = compaction.compact_text(combined, self._ctx, self._state)
        if not result.ok:
            return self._err(
                action="append",
                error_type="compaction_failed",
                message=result.error_message or "compaction failed",
            )
        if len(result.text) > MAX_CHARS:
            return self._err(
                action="append",
                error_type="limit_exceeded",
                message="content too large even after compaction.",
            )
        try:
            info = storage.write(self._path, result.text)
        except LongMemoryError as e:
            return self._err(action="append", error_type=e.code, message=e.message)
        return self._ok(
            action="append",
            length=info.chars,
            lines=info.lines,
            compaction_block={
                "performed": True,
                "before_chars": len(combined),
                "after_chars": len(result.text),
            },
            hint=_AUTO_COMPACTION_HINT,
        )

    def _handle_write(self, content: str) -> str:
        text = content if isinstance(content, str) else ""
        text_stripped = text.strip()
        if not text_stripped:
            return self._err(
                action="write",
                error_type="empty_content",
                message="content must be non-empty for write.",
            )
        if len(text_stripped) > MAX_CHARS:
            return self._err(
                action="write",
                error_type="input_too_long",
                message=(
                    f"content is {len(text_stripped)} chars (> {MAX_CHARS}). "
                    "Rewrite more concisely and retry."
                ),
            )
        try:
            info = storage.write(self._path, text_stripped)
        except LongMemoryError as e:
            return self._err(action="write", error_type=e.code, message=e.message)
        return self._ok(action="write", length=info.chars, lines=info.lines)


__all__ = ["LongMemory"]
