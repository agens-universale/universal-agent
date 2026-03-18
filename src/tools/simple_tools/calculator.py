from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool
import json
from typing import Any, Dict


class Calculator(AbstractSimpleTool):
    max_expression_chars = 2000

    @staticmethod
    def _result(
        *,
        ok: bool,
        expression: str,
        value: Any = None,
        value_type: str = "",
        error: Dict[str, Any] | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "calculator",
                "ok": ok,
                "expression": expression,
                "value": value,
                "value_type": value_type,
                "error": error,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "calculator",
            "description": (
                "Evaluates a Python expression (pure expression only) and returns a JSON string envelope. "
                "Returns ok=true with value on success, ok=false with error on failure. "
                "The expression length is limited to max_expression_chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": "The Python expression to evaluate, for example '1 + 2 * 3' or '(10 / 2) ** 3'."
                    }
                },
                "required": ["expression"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = f'''Tool name: calculator
Tool description: Evaluates a Python pure expression (no statements) and returns a JSON string.
Input schema: {{"type": "object", "properties": {{"expression": {{"type": "string", "description": "Python expression to evaluate."}}}}, "required": ["expression"], "additionalProperties": false}}

Usage:
- Use this tool for arithmetic and other side-effect-free Python expressions only.
- The expression must be a valid Python *expression* (no statements like for/while/if/def/etc.).
- Expression length must be <= {cls.max_expression_chars}.

Return format (JSON string):
{{
  "tool":"calculator",
  "ok": true|false,
  "expression": "...",
  "value": <result or null>,
  "value_type": "...",
  "error": null|{{"type":"...","message":"..."}}
}}
'''
        return description

    def __call__(self, expression: str, **kwargs) -> str:
        if not isinstance(expression, str):
            return self._result(
                ok=False,
                expression=str(expression),
                value=None,
                value_type="",
                error={"type": "invalid_argument", "field": "expression"},
            )

        if not expression.strip():
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={
                    "type": "invalid_argument",
                    "field": "expression",
                    "message": "Empty expression",
                },
            )

        if len(expression) > self.max_expression_chars:
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={"type": "expression_too_long", "message": f"Expression length exceeds {self.max_expression_chars} chars"},
            )

        try:
            # Evaluate expression in a restricted environment
            result = eval(expression, {"__builtins__": {}}, {})
            result_type = type(result).__name__
        except Exception as e:
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={"type": "eval_failed", "message": str(e)},
            )

        # Ensure value is JSON-serializable; if not, fall back to string representation.
        try:
            json.dumps(result, ensure_ascii=False)
            value = result
            value_type = result_type
        except Exception:
            value = str(result)
            value_type = "str"

        return self._result(
            ok=True, expression=expression, value=value, value_type=value_type, error=None
        )

