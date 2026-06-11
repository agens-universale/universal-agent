from tools.simple_tool_base import AbstractSimpleTool
import subprocess
import os
import json

from constants.tool_names import EXEC_COMMAND_NAME
from security.command_moderation import moderate_command_execution


class ExecuteCommand(AbstractSimpleTool):
    """
    Tool to execute a CLI command on the system and return its output.
    """
    # Default max chars per field (stdout/stderr) before truncation.
    cut_n = 4000
    timeout = 30

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "exec_command",
            "description": (
                "Executes a command synchronously (no background) and returns a JSON string envelope with "
                "fields: tool, ok, command, cwd, exit_code, timed_out, truncated, error, then stdout/stderr last. "
                "stdout/stderr are truncated to max_output_chars each."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command-line command to execute. Must be valid for the operating system and properly quoted, especially when paths contain spaces. Avoid interactive or destructive commands unless absolutely necessary."
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": f"Execution timeout in seconds. Default is {cls.timeout}.",
                        "minimum": 1,
                    },
                    "max_output_chars": {
                        "type": "integer",
                        "description": f"Max number of chars returned for stdout and stderr each. Default is {cls.cut_n}.",
                        "minimum": 1,
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory for this command (absolute or relative). "
                            "When unset, uses the current working directory."
                        ),
                    },
                    "env_vars": {
                        "type": "array",
                        "description": (
                            "Optional environment variables to add/override for this command. "
                            "Use [{name, value}] items. Values are converted to strings. "
                            "If omitted, uses the current process environment."
                        ),
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "value": {"type": "string"},
                            },
                            "required": ["name", "value"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": ["command"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls):
        description = ('''Tool name: exec_command
Tool description: Executes a CLI command synchronously and returns stdout/stderr/exit_code as JSON.
Input schema: {"type": "object", "properties": {"command": {"type": "string", "description": "The CLI command to execute."}, "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "default": 30}, "max_output_chars": {"type": "integer", "description": "Max chars returned for stdout/stderr each.", "default": 4000}, "cwd": {"type": "string", "description": "Optional working directory (absolute or relative)."}, "env_vars": {"type":"array","description":"Optional env overrides as [{name,value}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}}, "required": ["command"], "additionalProperties": false}

Return format (JSON string):
{"tool":"exec_command","ok":true|false,"command":"...","cwd":"...","exit_code":number|null,"timed_out":true|false,"truncated":true|false,"error":null|{"type":"...","message":"..."},"stdout":"...","stderr":"..."}

Usage:
- Use this tool to run shell commands such as listing directories (`ls`, `dir`), checking versions (`python --version`), or running scripts.
- Always quote arguments properly to avoid shell injection risks (the tool uses safe execution via subprocess).
- The command is executed in the current working directory.
- On success, the tool returns stdout; on failure, it returns stderr and exit code.
- Be cautious with destructive commands (e.g., `rm`, `del`, `rmdir`).
- Don't remove or create any files or folders outside of project directory!
- Avoid interactive commands (e.g., `vim`, `python` without args) — they will time out or fail.
- This tool is essential for inspecting system state, verifying installations, or automating workflows.
- It is very helpful if you write a clear, concise description of what this command does in 5-10 words.
- stdout/stderr are truncated to max_output_chars each (default ''' + str(cls.cut_n) + ''').

Important Usage Rules:
1. Directory Verification:
   - If the command will create new directories or files, be shure the parent directory exists and is the correct location. 
   - For example, before running "mkdir foo/bar", first use tool `list_files` to check that "foo" exists and is the intended parent directory.
2. Command Execution:
   - Always quote file paths that contain spaces with double quotes (e.g., cd "path with spaces/file.txt")
   - Examples of proper quoting:
     - cd "/Users/name/My Documents" (correct)
     - cd /Users/name/My Documents (incorrect - will fail)
     - python "/path/with spaces/script.py" (correct)
     - python /path/with spaces/script.py (incorrect - will fail)
   - After ensuring proper quoting, execute the command.
   - Capture the output of the command.
   - When issuing multiple commands, use the ';' or '&&' operator to separate them. DO NOT use newlines (newlines are ok in quoted strings
3. VERY IMPORTANT: You MUST avoid using search commands like `find` and `grep`. You MUST avoid read tools like `cat`, `head`, `tail`, and `ls`, and instead use filesystem tools (`fs_search_files`, `fs_list_files`, `fs_read_file`, `fs_search_and_replace`, …).
4. Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.
    <good-example>
    pytest /foo/bar/tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>
''')
        return description

    @staticmethod
    def _truncate_text(text: str, limit: int) -> tuple[str, bool]:
        if limit <= 0:
            return "", False
        if len(text) <= limit:
            return text, False
        cut = text[:limit]
        return cut, True

    @staticmethod
    def _result(
        *,
        ok: bool,
        command: str,
        cwd: str,
        exit_code: int | None,
        timed_out: bool,
        truncated: bool,
        stdout: str,
        stderr: str,
        error: dict | None = None,
    ) -> str:
        return json.dumps(
            {
                "tool": ExecuteCommand.registered_tool_name(),
                "ok": ok,
                "command": command,
                "cwd": cwd,
                "exit_code": exit_code,
                "timed_out": timed_out,
                "truncated": truncated,
                "error": error,
                "stdout": stdout,
                "stderr": stderr,
            },
            ensure_ascii=False,
        )

    def _command_moderation_blocked_result(
        self,
        error: dict,
        command: str,
        timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        **kwargs,
    ) -> str:
        return self._result(
            ok=False,
            command=command,
            cwd=cwd or os.getcwd(),
            exit_code=None,
            timed_out=False,
            truncated=False,
            stdout="",
            stderr="",
            error=error,
        )

    @moderate_command_execution(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    def __call__(
        self,
        command: str,
        timeout_seconds: int | None = None,
        max_output_chars: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        **kwargs,
    ) -> str:
        cwd_used = cwd or os.getcwd()

        # Input validation
        if not isinstance(command, str):
            return self._result(
                ok=False,
                command=str(command),
                cwd=cwd_used,
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "command"},
            )

        if not command.strip():
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "command", "message": "Empty command"},
            )

        if timeout_seconds is None:
            timeout_seconds = self.timeout
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )

        if max_output_chars is None:
            max_output_chars = self.cut_n
        if not isinstance(max_output_chars, int) or max_output_chars <= 0:
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "max_output_chars"},
            )

        if not isinstance(cwd_used, str) or not cwd_used.strip():
            return self._result(
                ok=False,
                command=command,
                cwd=str(cwd_used),
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "cwd"},
            )
        if not os.path.isdir(cwd_used):
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "cwd", "message": "Not a directory"},
            )

        merged_env = None
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return self._result(
                    ok=False,
                    command=command,
                    cwd=cwd_used,
                    exit_code=None,
                    timed_out=False,
                    truncated=False,
                    stdout="",
                    stderr="",
                    error={
                        "type": "invalid_argument",
                        "field": "env_vars",
                        "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
                    },
                )
            env_from_pairs: dict[str, str] = {}
            for idx, item in enumerate(env_vars):
                if not isinstance(item, dict):
                    return self._result(
                        ok=False,
                        command=command,
                        cwd=cwd_used,
                        exit_code=None,
                        timed_out=False,
                        truncated=False,
                        stdout="",
                        stderr="",
                        error={
                            "type": "invalid_argument",
                            "field": f"env_vars[{idx}]",
                            "message": "Each env_vars item must be an object: {'name': 'KEY', 'value': 'VALUE'}",
                        },
                    )
                name = item.get("name")
                if not isinstance(name, str) or not name.strip():
                    return self._result(
                        ok=False,
                        command=command,
                        cwd=cwd_used,
                        exit_code=None,
                        timed_out=False,
                        truncated=False,
                        stdout="",
                        stderr="",
                        error={"type": "invalid_argument", "field": f"env_vars[{idx}].name"},
                    )
                value = item.get("value")
                if not isinstance(value, str):
                    return self._result(
                        ok=False,
                        command=command,
                        cwd=cwd_used,
                        exit_code=None,
                        timed_out=False,
                        truncated=False,
                        stdout="",
                        stderr="",
                        error={
                            "type": "invalid_argument",
                            "field": f"env_vars[{idx}].value",
                            "message": "env_vars value must be a string",
                        },
                    )
                env_from_pairs[name.strip()] = value
            # Ensure string keys and values for subprocess env
            env_sanitized: dict[str, str] = {}
            for k, v in env_from_pairs.items():
                env_sanitized[str(k)] = "" if v is None else str(v)
            merged_env = {**os.environ, **env_sanitized}

        try:
            # На Windows запуск subprocess гасит ENABLE_MOUSE_INPUT родительской
            # консоли → TUI теряет мышь до перезапуска. Возвращаем режим после.
            from utils.win_console import preserve_console_mode
            with preserve_console_mode():
                result = subprocess.run(
                    command,
                    shell=True,
                    capture_output=True,
                    text=True,
                    timeout=timeout_seconds,
                    cwd=cwd_used,
                    encoding="utf-8",
                    errors="replace",
                    env=merged_env,
                )

            stdout_raw = result.stdout or ""
            stderr_raw = result.stderr or ""
            stdout, stdout_trunc = self._truncate_text(stdout_raw.rstrip(), max_output_chars)
            stderr, stderr_trunc = self._truncate_text(stderr_raw.rstrip(), max_output_chars)
            truncated = stdout_trunc or stderr_trunc

            exit_code = result.returncode
            ok = exit_code == 0
            error = None
            if not ok:
                error = {"type": "non_zero_exit", "message": f"Command exited with status {exit_code}"}

            return self._result(
                ok=ok,
                command=command,
                cwd=cwd_used,
                exit_code=exit_code,
                timed_out=False,
                truncated=truncated,
                stdout=stdout,
                stderr=stderr,
                error=error,
            )

        except subprocess.TimeoutExpired as e:
            stdout_raw = getattr(e, "stdout", "") or ""
            stderr_raw = getattr(e, "stderr", "") or ""
            stdout, _stdout_trunc = self._truncate_text(stdout_raw.rstrip(), max_output_chars)
            stderr, _stderr_trunc = self._truncate_text(stderr_raw.rstrip(), max_output_chars)
            truncated = _stdout_trunc or _stderr_trunc
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=None,
                timed_out=True,
                truncated=truncated,
                stdout=stdout,
                stderr=stderr,
                error={"type": "timeout", "message": f"Command timed out after {timeout_seconds} seconds"},
            )
        except Exception as e:
            err = str(e).replace("\n", " ").strip()
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used,
                exit_code=-1,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "execution_failed", "message": err},
            )