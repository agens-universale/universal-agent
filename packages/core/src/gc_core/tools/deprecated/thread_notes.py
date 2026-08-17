import json
from typing import Any, Dict, Optional

from gc_core.tools.simple_tool_base import AbstractSimpleTool


class ThreadNotes(AbstractSimpleTool):
    """
    Persistent per-thread markdown notes.

    Stored in threads meta_<thread_id>.json under a single field: notes_md.
    """

    tool_name = "thread_notes"
    MAX_CHARS = 50_000
    EMPTY_PROMPT = (
        "Notes are empty. Use this tool to persist meaningful decisions, specs, "
        "paths, or conclusions for this thread."
    )

    def __init__(
        self, ctx: Any = None, state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._state = state

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "thread_notes",
            "description": (
                "Read or update persistent markdown notes for the current thread. "
                "Notes persist across turns. "
                "This tool is the long-lived memory for the thread. "
                "Use it at the start of each user turn (action='read'). "
                "When you add new verified facts, ALWAYS use action='append' and provide ONLY the new delta "
                "(the tool will keep the existing notes). "
                "Use action='write' ONLY when you must rewrite notes from scratch "
                "(e.g., canonical target changed, or you detected an earlier entry is wrong). "
                "Store concise facts, evidence, and decisions; do NOT save long internal reasoning here."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "append", "write"],
                        "description": (
                            "read = return current notes; "
                            "append = add ONLY the new verified delta at the end; "
                            "write = replace completely with FULL updated notes."
                        ),
                    },
                    "content": {
                        "type": "string",
                        "description": (
                            "Markdown text for append/write. "
                            "For append: ONLY the new delta (do not resend the existing notes). "
                            "For write: the FULL updated markdown."
                        ),
                        "default": "",
                    },
                },
                "required": ["action"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls) -> str:
        return f'''Tool name: thread_notes
Tool description: Persistent per-thread markdown notes stored in thread meta. Can be read, appended, or rewritten.

Mandatory workflow (use it to avoid contradictions across turns):
- START of each new user turn: call `thread_notes` with action="read".
- If you discover/verify something important for this task: call thread_notes with action="append" (add a small factual update).
- If the user says your previous output is wrong / the canonical target changed: call thread_notes with action="write" (rewrite notes from scratch).

Recommended note structure (keep it concise):
- Canonical target (name + URL) and what it means for this task
- Evidence (URLs/files you fetched + key outcomes with result_type)
- Decisions (what you chose and why)
- Open questions / unknowns (what is still unverified)

What NOT to store:
- Long brainstorming / chain-of-thought
- Guesses presented as facts
Append safety rule:
- For action="append", the `content` MUST be ONLY the new delta (not the whole file), otherwise you'll get duplicated sections.
Write should be rare:
- For action="write", resend the entire updated notes only when you need a full rewrite/correction.
Input schema: {{"type":"object","properties":{{"action":{{"type":"string","enum":["read","append","write"]}}, "content":{{"type":"string","description":"Markdown text for append/write"}}}},"required":["action"],"additionalProperties":false}}

Usage:
- action="read": load current notes.
- action="append": add new information at the end.
- action="write": replace notes completely with updated markdown.

Return format (JSON string):
{{
  "tool":"thread_notes",
  "ok": true|false,
  "action": "read|append|write",
  "length": number,
  "error": null|{{"type":"...","message":"..."}},
  "notes_md": "..." (only for read; last field)
}}
'''

    @staticmethod
    def _result(
        *,
        ok: bool,
        action: str,
        length: int,
        notes_md: Optional[str] = None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "thread_notes",
                "ok": ok,
                "action": action,
                "length": length,
                "error": error,
                "notes_md": notes_md if action == "read" else None,
            },
            ensure_ascii=False,
        )

    def __call__(self, action: str, content: str = "", **kwargs: Any) -> str:
        state = self._state
        if state is None:
            return self._result(
                ok=False,
                action=action,
                length=0,
                notes_md=None,
                error={"type": "no_state", "message": "state is missing."},
            )

        shared = getattr(state, "shared", None)
        existing_md = getattr(shared, "notes_md", None) if shared is not None else None
        existing_md_stripped = (
            existing_md.strip() if isinstance(existing_md, str) else ""
        )

        if action not in ("read", "append", "write"):
            return self._result(
                ok=False,
                action=action,
                length=0,
                notes_md=None,
                error={"type": "invalid_action", "message": "Unsupported action."},
            )

        if action == "read":
            notes_md = (
                existing_md_stripped if existing_md_stripped else self.EMPTY_PROMPT
            )
            return self._result(
                ok=True,
                action=action,
                length=len(existing_md_stripped),
                notes_md=notes_md,
                error=None,
            )

        if not isinstance(content, str):
            content = str(content) if content is not None else ""

        if not content.strip():
            return self._result(
                ok=False,
                action=action,
                length=0,
                notes_md=None,
                error={"type": "empty_content", "message": "content must be non-empty for append/write."},
            )

        if action == "append":
            new_md = (
                (existing_md_stripped + "\n\n" + content.strip())
                if existing_md_stripped
                else content.strip()
            ).strip()
        else:  # write
            new_md = content.strip()

        if len(new_md) > self.MAX_CHARS:
            return self._result(
                ok=False,
                action=action,
                length=len(new_md),
                notes_md=None,
                error={"type": "limit_exceeded", "message": (f"Notes exceed {self.MAX_CHARS} chars. Not written. "
                                                             "Please read first and rewrite new version of notes with new content")},
            )

        state.shared.notes_md = new_md

        return self._result(
            ok=True,
            action=action,
            length=len(new_md),
            notes_md=None,
            error=None,
        )


__all__ = ["ThreadNotes"]
