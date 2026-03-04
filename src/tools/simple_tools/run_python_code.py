import os
import sys
import subprocess
import tempfile

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class RunPythonCode(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "run_python_code",
            "description": "Runs a given Python code snippet in a separate subprocess and returns its stdout, "
                           "stderr, and return code. Use this tool to execute small, self-contained Python "
                           "scripts for experimentation or calculations.",
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The full Python code to execute. It will be written to a temporary .py file "
                                       "and run with the current Python interpreter."
                    },
                    "timeout": {
                        "type": "integer",
                        "description": "Maximum execution time in seconds before the process is terminated.",
                        "default": 10
                    }
                },
                "required": ["code"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = '''Tool name: run_python_code
Tool description: Runs a Python code snippet in a separate subprocess and returns STDOUT, STDERR, and return code.
Input schema: {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to execute."}, "timeout": {"type": "integer", "description": "Execution timeout in seconds.", "default": 10}}, "required": ["code"], "additionalProperties": false}

Usage:
- Use this tool to execute small, self-contained Python scripts that do not require interaction.
- The code is written to a temporary .py file and executed with the current Python interpreter.
- Execution happens in a temporary directory with PYTHONPATH cleared to avoid unintended imports from the project.
- Do NOT use this tool for long-running tasks, interactive input, or accessing external systems in an unsafe way.
- If execution exceeds the timeout, the process is terminated and an error message is returned.
'''
        return description

    def __call__(self, code: str, timeout: int = 10, **kwargs) -> str:
        if not isinstance(code, str):
            return f"Error: Argument 'code' must be a string. Got: {type(code)} ({code})"

        if not isinstance(timeout, int):
            return f"Error: Argument 'timeout' must be an integer. Got: {type(timeout)} ({timeout})"

        if timeout <= 0:
            return "Error: Timeout must be a positive integer."

        if not code.strip():
            return "Error: Code cannot be empty or whitespace only."

        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                temp_file = f.name

            result = subprocess.run(
                [sys.executable, temp_file],
                capture_output=True,
                text=True,
                timeout=timeout,
                env={**os.environ, 'PYTHONPATH': ''},
                cwd=tempfile.gettempdir()
            )

            output_parts = []

            if result.stdout:
                output_parts.append(f"=== STDOUT ===\n{result.stdout}")

            if result.stderr:
                output_parts.append(f"=== STDERR ===\n{result.stderr}")

            output_parts.append(f"=== RETURN CODE ===\n{result.returncode}")

            return "\n\n".join(output_parts)

        except subprocess.TimeoutExpired:
            return f"Ошибка: Превышен таймаут выполнения ({timeout} сек)"
        except Exception as e:
            return f"Ошибка выполнения: {str(e)}"
        finally:
            if temp_file:
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass

