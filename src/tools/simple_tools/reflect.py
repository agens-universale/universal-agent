import json

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class Reflect(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        message: str | None,
        error: dict | None,
    ) -> str:
        return json.dumps(
            {
                "tool": "reflect",
                "ok": ok,
                "message": message,
                "error": error,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "reflect",
            "description": (
                "Think out loud during the current turn. "
                "This tool is for temporary in-dialogue thoughts only: it does NOT save anything "
                "to thread_notes or any storage. "
                "Reflection exists only in the current dialogue context."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": (
                            "Temporary thought text for this dialogue turn only. "
                            "Not persisted in thread_notes or any storage."
                        )
                    }
                },
                "required": ["content"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: reflect
Tool description: Think out loud for the current dialogue turn. This is temporary and not persisted.
IMPORTANT: reflect content is NOT saved to thread_notes and NOT saved to any storage. It only exists in the current dialogue context.
Input schema: {"type":"object","properties":{"content":{"type":"string"}},"required":["content"],"additionalProperties":false}

Return format (JSON string):
{"tool":"reflect","ok":true,"message":"...","error":null|{"type":"...","message":"..."}}
'''
        return description

    def __call__(self, content: str, **kwargs) -> str:
        if not isinstance(content, str):
            return self._result(
                ok=False,
                message=None,
                error={"type": "invalid_argument", "field": "content"},
            )

        return self._result(
            ok=True,
            message="Reflection added to the dialogue context. For persistent notes, use thread_notes or similar tools.",
            error=None,
        )
