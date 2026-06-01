import json

from tools.simple_tool_base import AbstractSimpleTool


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
                "tool": "think",
                "ok": ok,
                "error": error,
                "message": message,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "think",
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
    def prompt_description(cls):
        description = '''Tool name: think
Tool description: Think out loud for the current turn. Supports structured multi-layer thinking: thoughts, critics, doubts, reflection.
IMPORTANT: think content is NOT saved to thread memory and NOT saved to any durable storage. It only exists in the current dialogue context.
RULE: If your think output contains a specific Decision / Canonical target / Plan completed / Verified fact, record it separately via the thread memory tool (append) when one is available so it can persist.
Input schema: {"type":"object","properties":{"thoughts":{"type":"string"},"critics":{"type":"string"},"doubts":{"type":"string"},"reflection":{"type":"string"}},"additionalProperties":false}
At least one of thoughts, critics, doubts, reflection should be non-empty.

Return format (JSON string):
{"tool":"think","ok":true,"error":null|{"type":"...","message":"..."},"message":"..."}
'''
        return description

    def __call__(self, thoughts: str | None = None, critics: str | None = None, doubts: str | None = None, reflection: str | None = None, **kwargs) -> str:
        FIELDS = ("thoughts", "critics", "doubts", "reflection")
        vals = [thoughts, critics, doubts, reflection]
        for name, val in zip(FIELDS, vals):
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
