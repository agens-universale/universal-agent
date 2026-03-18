import json
from typing import Any, Dict, Optional

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


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
        self, ctx: Any = None, parent_state: Any = None, **kwargs: Any
    ) -> None:
        super().__init__(**kwargs)
        self._ctx = ctx
        self._parent_state = parent_state

    @classmethod
    def tool_description(cls) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": "thread_notes",
            "description": (
                "Read or update persistent markdown notes for the current thread. "
                "Notes survive across summarizations and can be rewritten entirely."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": ["read", "append", "write"],
                        "description": "read = return current notes; append = add at the end; write = replace completely.",
                    },
                    "content": {
                        "type": "string",
                        "description": "Markdown text for append/write.",
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
  "notes_md": "..." (only for read),
  "error": null|{{"type":"...","message":"..."}}
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
                "notes_md": notes_md if action == "read" else None,
                "error": error,
            },
            ensure_ascii=False,
        )

    def __call__(self, action: str, content: str = "", **kwargs: Any) -> str:
        state = self._parent_state
        if state is None:
            return self._result(
                ok=False,
                action=action,
                length=0,
                notes_md=None,
                error={"type": "no_state", "message": "parent_state is missing."},
            )

        existing_md = getattr(state, "notes_md", None)
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
                error={"type": "limit_exceeded", "message": f"Notes exceed {self.MAX_CHARS} chars."},
            )

        # Update in-memory state first.
        state.notes_md = new_md

        # Keep the metadata sent to backend in sync so the model sees updates.
        cm = getattr(state, "call_metadata", None)
        if isinstance(cm, dict):
            pv = cm.get("promptVariables")
            if isinstance(pv, dict):
                pv["notes_md"] = new_md
            else:
                cm["promptVariables"] = {"notes_md": new_md}

        return self._result(
            ok=True,
            action=action,
            length=len(new_md),
            notes_md=None,
            error=None,
        )


__all__ = ["ThreadNotes"]

