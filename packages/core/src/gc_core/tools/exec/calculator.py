from gc_core.loop_detector.constants import READ_PROFILE, loop_warn_on_repeat
from gc_core.tools.simple_tool_base import AbstractSimpleTool
import ast
import json
import operator
from typing import Any, Dict, Optional


# --- AST whitelist walker (C1, C5, C6) ---

_BIN_OPS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
    ast.Pow: operator.pow,
}

_UNARY_OPS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}


class _DisallowedSyntax(Exception):
    """Syntax was parsed but contains a node outside the whitelist (C6)."""


def _safe_eval(expression: str) -> Any:
    """Parse + walk expression under a strict AST whitelist.

    Two passes (C6 — "reject before evaluation"):
      1. ``_validate_node`` walks the *entire* tree and rejects any node outside
         the whitelist (names, calls, attributes, subscripts, containers,
         comprehensions, bool/compare, if-exp, str/bool/None constants, unknown).
         No computation happens here, so a forbidden right operand is caught
         before a heavy/side-effecting left operand is ever evaluated.
      2. ``_eval_node`` computes the now-validated tree; only runtime errors
         (e.g. ZeroDivisionError) can still surface here.

    No ``eval``/``exec`` is used (C1).
    """
    tree = ast.parse(expression, mode="eval")
    _validate_node(tree.body)
    return _eval_node(tree.body)


def _validate_node(node: ast.AST) -> None:
    """First pass: enforce the whitelist over the whole tree without computing."""
    # C5: numeric constants only — reject str/bool/None inside Constant.
    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool) or node.value is None or isinstance(node.value, str):
            raise _DisallowedSyntax(f"Constant of type {type(node.value).__name__!r} is not allowed")
        if isinstance(node.value, (int, float)):
            return
        raise _DisallowedSyntax(f"Constant of type {type(node.value).__name__!r} is not allowed")

    if isinstance(node, ast.BinOp):
        if type(node.op) not in _BIN_OPS:
            raise _DisallowedSyntax(f"BinOp operator {type(node.op).__name__} is not allowed")
        _validate_node(node.left)
        _validate_node(node.right)
        return

    if isinstance(node, ast.UnaryOp):
        if type(node.op) not in _UNARY_OPS:
            raise _DisallowedSyntax(f"UnaryOp operator {type(node.op).__name__} is not allowed")
        _validate_node(node.operand)
        return

    # C6: explicit deny of every other construct.
    raise _DisallowedSyntax(f"Node {type(node).__name__} is not allowed")


def _eval_node(node: ast.AST) -> Any:
    """Second pass: compute a tree already validated by ``_validate_node``.

    Runtime errors (e.g. ZeroDivisionError) propagate to the caller and map to
    ``eval_failed`` (C7). Unknown nodes are unreachable post-validation but
    rejected defensively.
    """
    if isinstance(node, ast.Constant):
        return node.value

    if isinstance(node, ast.BinOp):
        return _BIN_OPS[type(node.op)](_eval_node(node.left), _eval_node(node.right))

    if isinstance(node, ast.UnaryOp):
        return _UNARY_OPS[type(node.op)](_eval_node(node.operand))

    # Unreachable after validation; defensive guard.
    raise _DisallowedSyntax(f"Node {type(node).__name__} is not allowed")


@loop_warn_on_repeat(**READ_PROFILE)
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
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "exec_calculator",
            "description": (
                "Evaluates an arithmetic expression (int/float) with operators + - * / // % ** and unary +/-, "
                "plus parentheses. This is NOT Python and not a sandbox: there are no function calls, no names, "
                "no attribute access, no subscripts, no containers (list/tuple/set/dict), no comprehensions, "
                "no booleans, no strings, no comparisons. Disallowed syntax is rejected before evaluation. "
                "Returns ok=true with value on success, ok=false with error on failure. "
                "Expression length is limited to max_expression_chars."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "expression": {
                        "type": "string",
                        "description": (
                            "Arithmetic expression, e.g. '1 + 2 * 3', '(1+2+3)*7', '2**10', '-3.5/2'. "
                            "Only int/float literals and the operators + - * / // % ** with parentheses."
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
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = f'''Tool name: exec_calculator
Tool description: Evaluates an arithmetic expression (int/float literals + operators) and returns a JSON string. This is NOT Python — no sandbox, no builtins, no function calls, no names, no attribute access, no subscripts, no containers.
Input schema: {{"type": "object", "properties": {{"expression": {{"type": "string", "description": "Arithmetic expression (int/float literals and operators only)."}}}}, "required": ["expression"], "additionalProperties": false}}

Usage:
- Use for arithmetic only: int/float literals and the operators + - * / // % ** with unary +/- and parentheses.
- Examples: `(1+2+3)*7`, `2**10`, `-3.5/2`, `1 + 2 * 3`.
- NOT allowed and rejected before evaluation: function calls (sum, len, abs, …), names/identifiers, attribute access (`.__class__`, `.count`), subscripts (`x[0]`), containers (`[...]`, `(...)`, `{{...}}`, `{{...:...}}`), comprehensions, booleans, strings, None, comparisons.
- For anything beyond arithmetic, use `exec_python`.
- Expression length must be <= {cls.max_expression_chars}.

Return format (JSON string):
{{
  "tool":"exec_calculator",
  "ok": true|false,
  "expression": "...",
  "value_type": "int|float|...",
  "error": null|{{"type":"...","message":"..."}},
  "value": <number or null>
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
            result = _safe_eval(expression)
            result_type = type(result).__name__
        except _DisallowedSyntax as e:
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={"type": "disallowed_syntax", "message": str(e)},
            )
        except SyntaxError as e:
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={"type": "disallowed_syntax", "message": str(e)},
            )
        except Exception as e:
            # Execution-time errors (e.g. ZeroDivisionError) — keep envelope type
            # name ``eval_failed`` for compatibility (C7).
            return self._result(
                ok=False,
                expression=expression,
                value=None,
                value_type="",
                error={"type": "eval_failed", "message": str(e)},
            )

        return self._result(
            ok=True, expression=expression, value=result, value_type=result_type, error=None
        )

