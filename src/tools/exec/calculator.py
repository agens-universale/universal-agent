from tools.simple_tool_base import AbstractSimpleTool
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
                "tool": Calculator.registered_tool_name(),
                "ok": ok,
                "expression": expression,
                "value_type": value_type,
                "error": error,
                "value": value,
            },
            ensure_ascii=False,
        )

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "exec_calculator",
            "description": (
                "Evaluates a restricted Python expression (syntax only; not full Python). "
                "Allowed: numeric/string/bool/None literals, operators, literal containers "
                "(list/tuple/dict/set), subscripts, comprehensions, and conditional expressions. "
                "No global or builtin names (sum, len, float, abs, etc.), no imports, no statements. "
                "Method calls on a literal value (e.g. [1,2,3].count(1)) may work; prefer operators for math. "
                "Non-JSON values (e.g. set) are returned as strings. "
                "Returns ok=true with value on success, ok=false with error on failure. "
                "Expression length is limited to max_expression_chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "Restricted expression, e.g. '1 + 2 * 3', '(1+2+3)*7', '[1,2,3][0]*7'. "
                            "Global/builtin callables (sum, len, float, …) are unavailable."
                        ),
                    },
                },
                "required": ["expression"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = f'''Tool name: exec_calculator
Tool description: Evaluates a restricted Python expression (literal/operator sandbox; not full Python) and returns a JSON string.
Input schema: {{"type": "object", "properties": {{"expression": {{"type": "string", "description": "Restricted expression (literals and operators only)."}}}}, "required": ["expression"], "additionalProperties": false}}

Usage:
- Use for arithmetic and similar calculations without shell or code execution.
- Syntax is Python *expression* form only (no statements: for/while/if/def/import/etc.).
- No global/builtin names: sum(), len(), float(), abs(), etc. will fail — use operators and literals (e.g. `(1+2+3)*7`, `[1,2,3][0]*7`).
- Comprehensions and `x if cond else y` are allowed; method calls on a literal (e.g. `[1,2,3].count(1)`) may work but are not required for arithmetic.
- set and other non-JSON results come back as strings in `value`.
- Expression length must be <= {cls.max_expression_chars}.

Return format (JSON string):
{{
  "tool":"exec_calculator",
  "ok": true|false,
  "expression": "...",
  "value_type": "...",
  "error": null|{{"type":"...","message":"..."}},
  "value": <result or null>
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

