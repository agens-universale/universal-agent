import os
import sys
import subprocess
import tempfile
import json
from typing import Optional, Dict, Any

from tools.simple_tools.abstract_simple_tool import AbstractSimpleTool


class RunPythonCode(AbstractSimpleTool):
    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "run_python_code",
            "description": (
                "Runs Python code synchronously (no background) in a subprocess and returns a JSON string envelope "
                "with: tool, ok, return_code, timed_out, truncated, stdout, stderr, error. "
                "stdout/stderr are truncated to max_output_chars each."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": "The full Python code to execute. It will be written to a temporary .py file "
                                       "and run with the current Python interpreter."
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": "Maximum execution time in seconds before the process is terminated.",
                        "default": 10
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": "Max chars returned for stdout and stderr each.",
                        "default": 4000,
                        "minimum": 1,
                    },
                    "cwd": {
                        "type": "string",
                        "description": "Optional working directory for the subprocess. Defaults to temp directory.",
                    },
                    "env": {
                        "type": "object",
                        "description": "Optional environment variables to add/override. Values must be strings.",
                        "additionalProperties": {"type": "string"},
                    },
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
Tool description: Runs a Python code snippet in a subprocess and returns stdout/stderr as JSON.
Input schema: {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to execute."}, "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "default": 10}, "max_output_chars": {"type":"integer","description":"Max chars for stdout/stderr each.","default": 4000}, "cwd": {"type":"string","description":"Working directory (optional)"}, "env": {"type":"object","description":"Env overrides (optional)"}} , "required": ["code"], "additionalProperties": false}

Usage:
- Use this tool to execute small, self-contained Python scripts that do not require interaction.
- The code is written to a temporary .py file and executed with the current Python interpreter.
- Execution happens in a temporary directory with PYTHONPATH cleared to avoid unintended imports from the project.
- Do NOT use this tool for long-running tasks, interactive input, or accessing external systems in an unsafe way.
- If execution exceeds the timeout, the process is terminated and an error message is returned.

Return format (JSON string):
{
  "tool":"run_python_code",
  "ok": true|false,
  "return_code": number|null,
  "timed_out": true|false,
  "truncated": true|false,
  "stdout": "...",
  "stderr": "...",
  "error": null|{"type":"...","message":"..."}
}
'''
        return description

    @staticmethod
    def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
        if limit <= 0:
            return "", False
        if len(text) <= limit:
            return text, False
        return text[:limit], True

    @staticmethod
    def _result(
        *,
        ok: bool,
        return_code: Optional[int],
        timed_out: bool,
        truncated: bool,
        stdout: str,
        stderr: str,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        return json.dumps(
            {
                "tool": "run_python_code",
                "ok": ok,
                "return_code": return_code,
                "timed_out": timed_out,
                "truncated": truncated,
                "stdout": stdout,
                "stderr": stderr,
                "error": error,
            },
            ensure_ascii=False,
        )

    def __call__(
        self,
        code: str,
        timeout_seconds: Optional[int] = None,
        max_output_chars: int = 4000,
        cwd: Optional[str] = None,
        env: Optional[Dict[str, Any]] = None,
        **kwargs,
    ) -> str:
        if not isinstance(code, str):
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "code"},
            )

        if not code.strip():
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "code", "message": "Code is empty"},
            )

        effective_timeout = timeout_seconds if timeout_seconds is not None else 10
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )

        if not isinstance(max_output_chars, int) or max_output_chars <= 0:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "max_output_chars"},
            )

        if cwd is None:
            cwd_used = tempfile.gettempdir()
        else:
            cwd_used = cwd
            if not isinstance(cwd_used, str) or not cwd_used.strip():
                return self._result(
                    ok=False,
                    return_code=None,
                    timed_out=False,
                    truncated=False,
                    stdout="",
                    stderr="",
                    error={"type": "invalid_argument", "field": "cwd"},
                )

        temp_file = None
        try:
            with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
                f.write(code)
                temp_file = f.name

            result = subprocess.run(
                [sys.executable, temp_file],
                capture_output=True,
                text=True,
                timeout=effective_timeout,
                env=_sanitize_env(env),
                cwd=cwd_used,
            )

            stdout_raw = result.stdout or ""
            stderr_raw = result.stderr or ""
            stdout, stdout_trunc = self._truncate_text(stdout_raw.rstrip(), max_output_chars)
            stderr, stderr_trunc = self._truncate_text(stderr_raw.rstrip(), max_output_chars)
            truncated = stdout_trunc or stderr_trunc

            return_code = result.returncode
            ok = return_code == 0

            error = None
            if not ok:
                error = {"type": "non_zero_exit", "message": f"Process exited with status {return_code}"}

            return self._result(
                ok=ok,
                return_code=return_code,
                timed_out=False,
                truncated=truncated,
                stdout=stdout,
                stderr=stderr,
                error=error,
            )

        except subprocess.TimeoutExpired:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=True,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "timeout", "message": f"Execution timed out after {effective_timeout} seconds"},
            )
        except Exception as e:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "execution_failed", "message": str(e)},
            )
        finally:
            if temp_file:
                try:
                    os.unlink(temp_file)
                except Exception:
                    pass

        # Should not reach here
        return self._result(
            ok=False,
            return_code=None,
            timed_out=False,
            truncated=False,
            stdout="",
            stderr="",
            error={"type": "internal_error", "message": "Unexpected end"},
        )


def _sanitize_env(env: Optional[Dict[str, Any]]) -> Dict[str, str]:
    base_env = {**os.environ, "PYTHONPATH": ""}
    if env is None:
        return base_env
    if not isinstance(env, dict):
        return base_env
    sanitized: Dict[str, str] = {}
    for k, v in env.items():
        if k is None:
            continue
        key = k if isinstance(k, str) else str(k)
        if v is None:
            sanitized[key] = ""
        elif isinstance(v, str):
            sanitized[key] = v
        else:
            sanitized[key] = str(v)
    return {**base_env, **sanitized}

