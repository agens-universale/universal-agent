from gc_core.tools.simple_tool_base import AbstractSimpleTool
import json
from typing import Any, Dict, Optional

from gc_core.config_modules.agent import DEFAULT_EXEC_TIMEOUT_SEC, exec_timeout_from_config_dict
from gc_core.backends.capture import CapturedStream
from gc_core.config_modules.agent import exec_output_preview_edge_chars_from_config_dict
from gc_core.constants.tool_names import EXEC_COMMAND_NAME
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import moderate_command_execution
from gc_core.tools.exec.exec_envelope import build_exec_envelope


class ExecuteCommand(AbstractSimpleTool):
    """
    Tool to execute a CLI command on the system and return its output.
    """
    # Fallback when config_dict has no exec_timeout_sec (tests / partial wiring).
    timeout = DEFAULT_EXEC_TIMEOUT_SEC

    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = {
            "type": "function",
            "name": "exec_command",
            "description": (
                "Run a shell command synchronously. Returns JSON with ok, exit_code, stdout, stderr."
                + ' Each stream is an object: a whole stream has `content`, a stream whose middle was dropped has `head`, `tail`, `total_chars` and `omitted_chars`. A long stream also gets a `preview` block with its first and last chunk, placed before the full channels. The dropped middle is gone — redirect to a file before the run if you need the whole log.'
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
                        "description": (
                            f"Execution timeout in seconds. Default is {DEFAULT_EXEC_TIMEOUT_SEC} "
                            "(agent.exec_timeout_sec)."
                        ),
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
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        description = ('''Tool name: exec_command
Tool description: Run a shell command synchronously; returns JSON with exit_code, stdout, stderr.
Input schema: {"type": "object", "properties": {"command": {"type": "string", "description": "The CLI command to execute."}, "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "default": ''' + str(DEFAULT_EXEC_TIMEOUT_SEC) + '''}, "cwd": {"type": "string", "description": "Optional working directory (absolute or relative)."}, "env_vars": {"type":"array","description":"Optional env overrides as [{name,value}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}}, "required": ["command"], "additionalProperties": false}

Return format (JSON string):
{"tool":"exec_command","ok":true|false,"command":"...","cwd":"...","exit_code":number|null,"timed_out":true|false,"truncated":true|false,"error":null|{"type":"...","message":"..."},"preview":{"stdout":{"head":"...","tail":"...","total_chars":number,"omitted_chars":number}},"stdout":{"truncated":false,"content":"...","total_chars":number},"stderr":{"truncated":true,"head":"...","tail":"...","total_chars":number,"omitted_chars":number}}
`preview` appears only for a stream longer than two preview edges and only lists such streams; `stdout`/`stderr` carry `content` when whole and `head`+`tail` when the middle was dropped.

Usage:
- Use this tool to run shell commands such as listing directories (`ls`, `dir`), checking versions (`python --version`), or running scripts.
- Always quote arguments properly to avoid shell injection risks (the tool uses safe execution via subprocess).
- The command is executed in the current working directory.
- Always returns both stdout and stderr; check ok / exit_code for success or failure.
- stdout / stderr are objects: `content` for a whole stream, or `head` + `tail` +
  `total_chars` + `omitted_chars` when the middle was dropped. A long stream also appears
  in a leading `preview` block (first and last chunk). The dropped middle is not
  recoverable: if you need the full log, redirect it to a file before running, e.g.
  `pytest > .gc-pytest.log 2>&1; status=$?; tail -n 200 .gc-pytest.log; exit $status`.
- Be cautious with destructive commands (e.g., `rm`, `del`, `rmdir`).
- Don't remove or create any files or folders outside of project directory!
- Avoid interactive commands (e.g., `vim`, `python` without args) — they will time out or fail.
- This tool is essential for inspecting system state, verifying installations, or automating workflows.
- It is very helpful if you write a clear, concise description of what this command does in 5-10 words.

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
3. Read and search file contents with the filesystem tools (`fs_read_file`, `fs_find_in_files`, `fs_find_files_by_name`, `fs_list_files`, `fs_search_and_replace`, …): they are permission-checked and return structured results. Do NOT use shell `cat`/`head`/`tail`/`grep`/`rg`/`find` to read or search file contents — use those tools instead. Shell is right for everything else; remember exec stdout/stderr are captured as first+last chunks per stream and the middle of a very long stream is dropped.
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
    def _as_stream(value) -> CapturedStream:
        if isinstance(value, CapturedStream):
            return value
        text = value or ""
        return CapturedStream(head=text, total_chars=len(text))

    def _result(
        self,
        *,
        ok: bool,
        command: str,
        cwd: str,
        exit_code: int | None,
        timed_out: bool,
        stdout=None,
        stderr=None,
        error: dict | None = None,
    ) -> str:
        """Envelope с front-loaded preview; ``truncated`` выводится из потоков."""
        return build_exec_envelope(
            tool=ExecuteCommand.registered_tool_name(),
            ok=ok,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=self._as_stream(stdout),
            stderr=self._as_stream(stderr),
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(self._config_dict),
            error=error,
            head_fields={"command": command, "cwd": cwd},
        )

    def _command_moderation_blocked_result(
        self,
        error: dict,
        command: str,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        **kwargs,
    ) -> str:
        return self._result(
            ok=False,
            command=command,
            cwd=cwd or (self._backend.default_cwd() or ""),
            exit_code=None,
            timed_out=False,
            stdout="",
            stderr="",
            error=error,
        )

    @enforce_command_guard(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    @moderate_command_execution(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    def __call__(
        self,
        command: str,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        **kwargs,
    ) -> str:
        cwd_used = cwd if cwd is not None else self._backend.default_cwd()

        # Input validation
        if not isinstance(command, str):
            return self._result(
                ok=False,
                command=str(command),
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "command"},
            )

        if not command.strip():
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "command", "message": "Empty command"},
            )

        if timeout_seconds is None:
            timeout_seconds = exec_timeout_from_config_dict(self._config_dict)
        if not isinstance(timeout_seconds, int) or timeout_seconds <= 0:
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )


        if cwd_used is not None:
            if not isinstance(cwd_used, str) or not cwd_used.strip():
                return self._result(
                    ok=False,
                    command=command,
                    cwd=str(cwd_used),
                    exit_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={"type": "invalid_argument", "field": "cwd"},
                )
            if not self._backend.is_dir(cwd_used):
                return self._result(
                    ok=False,
                    command=command,
                    cwd=cwd_used,
                    exit_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={"type": "invalid_argument", "field": "cwd", "message": "Not a directory"},
                )

        env_sanitized: dict[str, str] = {}
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return self._result(
                    ok=False,
                    command=command,
                    cwd=cwd_used or "",
                    exit_code=None,
                    timed_out=False,
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
                        cwd=cwd_used or "",
                        exit_code=None,
                        timed_out=False,
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
                        cwd=cwd_used or "",
                        exit_code=None,
                        timed_out=False,
                        stdout="",
                        stderr="",
                        error={"type": "invalid_argument", "field": f"env_vars[{idx}].name"},
                    )
                value = item.get("value")
                if not isinstance(value, str):
                    return self._result(
                        ok=False,
                        command=command,
                        cwd=cwd_used or "",
                        exit_code=None,
                        timed_out=False,
                        stdout="",
                        stderr="",
                        error={
                            "type": "invalid_argument",
                            "field": f"env_vars[{idx}].value",
                            "message": "env_vars value must be a string",
                        },
                    )
                env_from_pairs[name.strip()] = value
            env_sanitized = env_from_pairs

        try:
            res = self._backend.exec(
                command, cwd=cwd_used, timeout=timeout_seconds, env=env_sanitized or None
            )
        except Exception as e:
            err = str(e).replace("\n", " ").strip()
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used or "",
                exit_code=-1,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "execution_failed", "message": err},
            )

        # getattr, а не атрибут: backend вправе вернуть свой результат-подобный
        # объект без структуры потока — тогда падаем обратно на плоский текст,
        # а не роняем тулу AttributeError уже после исполнения команды.
        stdout_stream = self._as_stream(
            getattr(res, "stdout_stream", None) or getattr(res, "stdout", "")
        )
        stderr_stream = self._as_stream(
            getattr(res, "stderr_stream", None) or getattr(res, "stderr", "")
        )

        if res.timed_out:
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=True,
                stdout=stdout_stream,
                stderr=stderr_stream,
                error={"type": "timeout", "message": f"Command timed out after {timeout_seconds} seconds"},
            )

        exit_code = res.return_code
        ok = exit_code == 0
        error = None if ok else {"type": "non_zero_exit", "message": f"Command exited with status {exit_code}"}
        return self._result(
            ok=ok,
            command=command,
            cwd=cwd_used or "",
            exit_code=exit_code,
            timed_out=False,
            stdout=stdout_stream,
            stderr=stderr_stream,
            error=error,
        )
