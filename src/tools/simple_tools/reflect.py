import json

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class Reflect(AbstractSimpleTool):
    @staticmethod
    def _result(
        *,
        ok: bool,
        content: str,
        truncated: bool,
        length: int,
        error: dict | None,
    ) -> str:
        return json.dumps(
            {
                "tool": "reflect",
                "ok": ok,
                "content": content,
                "truncated": truncated,
                "length": length,
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
                "Capture an in-turn reflection (think out loud). "
                "Returned content is included into dialogue context, but it is NOT persisted anywhere "
                "and is not visible to other threads/agents."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "content": {
                        "type": "string",
                        "description": "Reflection text to include into the dialogue context."
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
Tool description: Use it to include a short reflection into the dialogue (not persisted).
Input schema: {"type":"object","properties":{"content":{"type":"string"}},"required":["content"],"additionalProperties":false}

Return format (JSON string):
{"tool":"reflect","ok":true,"content":"...","truncated":true|false,"length":number,"error":null|{"type":"...","message":"..."}}
'''
        return description

    def __call__(self, content: str, **kwargs) -> str:
        if not isinstance(content, str):
            return self._result(
                ok=False,
                content="",
                truncated=False,
                length=0,
                error={"type": "invalid_argument", "field": "content"},
            )

        trimmed = content.strip()
        max_chars = 8000
        truncated = False
        if len(trimmed) > max_chars:
            trimmed = trimmed[:max_chars]
            truncated = True

        return self._result(
            ok=True,
            content=trimmed,
            truncated=truncated,
            length=len(trimmed),
            error=None,
        )
