"""Structured task checklist for the current session (``session_todo``).

SoT — ``state.session_todo``; every call fully replaces the list. The orchestrator's
list lives in the shared block (persisted to thread meta, rendered by TUI as a
checklist); a subagent keeps its own, ephemeral one — see ``State.session_todo``.
"""

import json
from typing import Any, Dict, List, Optional

from gc_core.constants.tool_names import SESSION_TODO_TOOL_NAME
from gc_core.loop_detector.constants import GENERIC_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool


def _todo_statuses():
    # Deferred to avoid circular import: persistence.__init__ → state_serializer → states → tools
    from gc_core.persistence.schema import TODO_STATUSES
    return TODO_STATUSES

def _counts(items: List[Dict[str, str]]) -> Dict[str, int]:
    statuses = _todo_statuses()
    tally = dict.fromkeys(statuses, 0)
    for i in items:
        s = i["status"]
        if s in tally:
            tally[s] += 1
    return {
        "count": len(items),
        "completed": tally.get("completed", 0),
        "in_progress": tally.get("in_progress", 0),
        "pending": tally.get("pending", 0),
    }


@loop_warn_on_repeat(**GENERIC_PROFILE)
class SessionTodo(AbstractSimpleTool):
    tool_name = SESSION_TODO_TOOL_NAME

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
        return {
            "type": "function",
            "name": SESSION_TODO_TOOL_NAME,
            "description": (
                "Task checklist for the CURRENT task — your plan and progress tracker. "
                "USE IT PROACTIVELY, without being asked: write the checklist BEFORE "
                "starting work whenever the task has 3+ logical steps, several "
                "deliverables, or a list of requirements to satisfy. Planning on paper "
                "first is how you avoid dropping requirements halfway through — do not "
                "rely on memory for multi-step work. "
                "Then keep it current as you go: mark a step completed right after "
                "finishing it and set the next one to in_progress (exactly one "
                "in_progress at a time); add steps when new work surfaces. "
                "Each call FULLY REPLACES the checklist with the list you pass; replace "
                "the whole list when the task itself changes, pass an empty list to "
                "clear it. "
                "Skip the checklist only for genuinely trivial or single-step tasks. "
                "It survives dialog summarization and is shown to the user as live "
                "progress."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "todos": {
                        "type": "array",
                        "description": (
                            "Full replacement checklist: every remaining and completed "
                            "step of the current task, in execution order."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "content": {
                                    "type": "string",
                                    "description": "Step formulation (imperative, concise).",
                                },
                                "status": {
                                    "type": "string",
                                    "enum": list(_todo_statuses()),
                                },
                            },
                            "required": ["content", "status"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["todos"],
                "additionalProperties": False,
            },
            "strict": True,
        }

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None) -> str:
        return """Tool name: session_todo
Tool description: Structured checklist of the CURRENT task (plan + progress). Each call fully replaces the list; empty list clears it. Statuses: pending | in_progress | completed.

Usage:
- USE IT PROACTIVELY, without being asked. Write the checklist BEFORE starting work whenever the task has 3+ logical steps, several deliverables, or a list of requirements to satisfy.
- Planning on paper first is how you avoid dropping requirements halfway through — do not rely on memory for multi-step work.
- Skip it only for genuinely trivial or single-step tasks.
- Update it after completing each step; add steps when new work surfaces.
- Keep exactly one item in_progress at a time.
- Always send ALL items (completed ones included) — the call replaces the previous list.
- If the user's request changes the task, replace the list; if the checklist became irrelevant, clear it with an empty list.
- The checklist is persisted across dialog turns and summarization, and is rendered to the user as live progress.

Input schema: {"type":"object","properties":{"todos":{"type":"array","items":{"type":"object","properties":{"content":{"type":"string"},"status":{"type":"string","enum":["pending","in_progress","completed"]}},"required":["content","status"],"additionalProperties":false}}},"required":["todos"],"additionalProperties":false}
"""

    def _result(self, *, ok: bool, items: List[Dict[str, str]],
                message: str, error: Optional[Dict[str, Any]] = None) -> str:
        payload: Dict[str, Any] = {
            "tool": SESSION_TODO_TOOL_NAME,
            "ok": ok,
            "error": error,
            "message": message,
        }
        payload.update(_counts(items))
        return json.dumps(payload, ensure_ascii=False)

    def _error(self, error_type: str, message: str) -> str:
        current = self._current_items()
        return self._result(
            ok=False,
            items=current,
            message="Checklist NOT changed.",
            error={"type": error_type, "message": message},
        )

    def _current_items(self) -> List[Dict[str, str]]:
        raw = getattr(self._state, "session_todo", None)
        return raw if isinstance(raw, list) else []


    def _validate_item(self, index: int, item: Any) -> tuple:
        """``(ответ_с_ошибкой, None)`` либо ``(None, нормализованный_пункт)``."""
        if not isinstance(item, dict):
            return self._error("invalid_item", f"todos[{index}] must be an object."), None
        extra = set(item) - {"content", "status"}
        if extra:
            return self._error(
                "invalid_item",
                f"todos[{index}] has unexpected fields: {', '.join(sorted(extra))}.",
            ), None
        content = item.get("content")
        status = item.get("status")
        if not isinstance(content, str) or not content.strip():
            return self._error(
                "invalid_item",
                f"todos[{index}].content must be a non-empty string.",
            ), None
        if status not in _todo_statuses():
            return self._error(
                "invalid_item",
                f"todos[{index}].status must be one of: "
                f"{', '.join(_todo_statuses())}.",
            ), None
        return None, {"content": content.strip(), "status": status}

    async def acall(self, todos: Any = None, **kwargs: Any) -> str:
        if self._state is None or getattr(self._state, "shared", None) is None:
            return json.dumps({
                "tool": SESSION_TODO_TOOL_NAME,
                "ok": False,
                "error": {"type": "no_state", "message": "state is missing."},
                "message": "Checklist NOT changed.",
            }, ensure_ascii=False)
        if kwargs:
            return self._error(
                "unexpected_arguments",
                f"Unexpected arguments: {', '.join(sorted(kwargs.keys()))}.",
            )
        if not isinstance(todos, list):
            return self._error(
                "invalid_todos",
                "todos must be an array of {content, status} objects "
                "(empty array clears the checklist).",
            )

        normalized: List[Dict[str, str]] = []
        for index, item in enumerate(todos):
            error, entry = self._validate_item(index, item)
            if error is not None:
                return error
            normalized.append(entry)

        self._state.session_todo = normalized
        counts = _counts(normalized)
        if not normalized:
            message = "Checklist cleared."
        else:
            message = (
                f"Checklist replaced: {counts['count']} items "
                f"({counts['completed']} completed, {counts['in_progress']} in progress, "
                f"{counts['pending']} pending)."
            )
        return self._result(ok=True, items=normalized, message=message)
