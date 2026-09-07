import json
from typing import Any, Dict, Optional

from gc_core.loop_detector.constants import GENERIC_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool

_THINK_FIELDS = ("thoughts", "critics", "doubts", "reflection")


@loop_warn_on_repeat(**GENERIC_PROFILE)
class Think(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        message: str | None,
        error: dict | None,
    ) -> str:
        return json.dumps(
            {
                "tool": Think.registered_tool_name(),
                "ok": ok,
                "error": error,
                "message": message,
            },
            ensure_ascii=False,
        )

    @classmethod
    def prepare_args_before_strip(cls, args: Dict[str, Any]) -> Dict[str, Any]:
        """If only injected ``reason`` is filled, treat it as ``thoughts`` before strip."""
        if not isinstance(args, dict):
            return args
        reason = args.get("reason")
        if not isinstance(reason, str) or not reason.strip():
            return args
        if any(
            isinstance(args.get(name), str) and args.get(name, "").strip()
            for name in _THINK_FIELDS
        ):
            return args
        remapped = dict(args)
        remapped["thoughts"] = reason
        return remapped

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "session_think",
            "description": (
                "Think out loud during the current turn. Use it when complex reasoning, "
                "self-critique, or structured reflection is needed. "
                "Content is NOT saved to thread memory or any other durable storage—it only "
                "exists in the current dialogue context. "
                "If your think output contains a specific Decision / Canonical target / "
                "Plan completed / Verified fact, record it separately via the thread memory "
                "tool (append) when one is available so it can persist across summarization."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "thoughts": {
                        "type": "string",
                        "description": "Reasoning, hypotheses, or exploration of ideas.",
                    },
                    "critics": {
                        "type": "string",
                        "description": "Self-critique: what could be wrong, weak points, alternative views.",
                    },
                    "doubts": {
                        "type": "string",
                        "description": "Uncertainties, open questions, or things that need verification.",
                    },
                    "reflection": {
                        "type": "string",
                        "description": "Synthesis or meta-reflection: main takeaways, conclusions, next steps.",
                    },
                },
                "required": [],
                "additionalProperties": False,
            },
            "strict": True,
        }
        return description

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = '''Tool name: session_think
Tool description: Think out loud for the current turn. Supports structured multi-layer thinking: thoughts, critics, doubts, reflection.
IMPORTANT: think content is NOT saved to thread memory and NOT saved to any durable storage. It only exists in the current dialogue context.
RULE: If your think output contains a specific Decision / Canonical target / Plan completed / Verified fact, record it separately via the thread memory tool (append) when one is available so it can persist.
If only injected `reason` is filled, it is treated as `thoughts`.
Input schema: {"type":"object","properties":{"thoughts":{"type":"string"},"critics":{"type":"string"},"doubts":{"type":"string"},"reflection":{"type":"string"}},"additionalProperties":false}
At least one of thoughts, critics, doubts, reflection should be non-empty.

Return format (JSON string):
{"tool":"session_think","ok":true,"error":null|{"type":"...","message":"..."},"message":"..."}
'''
        return description

    def __call__(self, thoughts: str | None = None, critics: str | None = None, doubts: str | None = None, reflection: str | None = None, **kwargs) -> str:
        vals = [thoughts, critics, doubts, reflection]
        for name, val in zip(_THINK_FIELDS, vals):
            if val is not None and not isinstance(val, str):
                return self._result(
                    ok=False,
                    message=None,
                    error={"type": "invalid_argument", "field": name},
                )
        if not any(isinstance(v, str) and v.strip() for v in vals):
            return self._result(
                ok=False,
                message=None,
                error={
                    "type": "invalid_argument",
                    "message": "At least one of thoughts, critics, doubts, reflection must be non-empty.",
                },
            )

        return self._result(
            ok=True,
            message=(
                "Thinking added to the dialogue context only; it is not written to "
                "durable storage. If this output includes a decision, verified fact, "
                "plan milestone, or other takeaway worth keeping across turns and "
                "summarization, append it to thread memory with the available "
                "persistence tool before continuing."
            ),
            error=None,
        )
