from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class Calculator(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "calculator",
            "description": "Evaluates a given Python expression using eval and returns the result as a string. "
                           "Supports basic arithmetic and other pure Python expressions. "
                           "Do not use for executing arbitrary code or accessing the filesystem.",
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
        description = '''Tool name: calculator
Tool description: Evaluates a Python expression using eval and returns the result as a string.
Input schema: {"type": "object", "properties": {"expression": {"type": "string", "description": "Python expression to evaluate, e.g. \\"1 + 2 * 3\\"."}}, "required": ["expression"], "additionalProperties": false}

Usage:
- Use this tool to quickly evaluate arithmetic or other simple Python expressions.
- The expression must be a valid Python expression (no statements like for/while/if/def/etc.).
- Do NOT use this tool to access the filesystem, environment variables, network, or other side effects.
- This tool is intended only for pure calculations and simple expression evaluation.
'''
        return description

    def __call__(self, expression: str, **kwargs) -> str:
        if not isinstance(expression, str):
            return f"Error: Argument 'expression' must be a string. Got: {type(expression)} ({expression})"

        if not expression.strip():
            return "Error: Expression cannot be empty or whitespace only."

        try:
            # Evaluate expression in a restricted environment
            result = eval(expression, {"__builtins__": {}}, {})
        except Exception as e:
            return f"Error while evaluating expression: {e}"

        return str(result)

