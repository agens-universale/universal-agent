from tools.simple_tool_base import AbstractSimpleTool
import json

from config_modules.agent import DEFAULT_EXEC_TIMEOUT_SEC, exec_timeout_from_config_dict
from constants.tool_names import EXEC_COMMAND_NAME, READ_STORED_TOOL_OUTPUT_NAME
from security.command_guard import enforce_command_guard
from security.command_moderation import moderate_command_execution
from tools.exec.stream_preview import (
    apply_stream_meta_to_envelope,
    clamp_max_output_chars,
    process_exec_stream,
)


class ExecuteCommand(AbstractSimpleTool):
    """
    Tool to execute a CLI command on the system and return its output.
    """
    # Preview budget for stdout/stderr (не в tool schema — политика рантайма).
    cut_n = 4000
    # Fallback when config_dict has no exec_timeout_sec (tests / partial wiring).
    timeout = DEFAULT_EXEC_TIMEOUT_SEC

    @classmethod
    def tool_description(cls):
        description = {
            "type": "function",
            "name": "exec_command",
            "description": (
                "Run a shell command synchronously. Returns JSON with ok, exit_code, stdout, stderr."
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
    def prompt_description(cls):
        description = ('''Tool name: exec_command
Tool description: Run a shell command synchronously; returns JSON with exit_code, stdout, stderr.
Input schema: {"type": "object", "properties": {"command": {"type": "string", "description": "The CLI command to execute."}, "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "default": ''' + str(DEFAULT_EXEC_TIMEOUT_SEC) + '''}, "cwd": {"type": "string", "description": "Optional working directory (absolute or relative)."}, "env_vars": {"type":"array","description":"Optional env overrides as [{name,value}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}}, "required": ["command"], "additionalProperties": false}

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
    def _process_stream(
        captured: str,
        max_output_chars: int,
        *,
        hit_capture_cap: bool,
        state,
        call_id,
        tool_name: str,
    ) -> dict:
        """D3+D4+D5: head+tail preview + optional store.put + output_ref."""
        return process_exec_stream(
            captured,
            max_output_chars,
            hit_capture_cap=hit_capture_cap,
            state=state,
            call_id=call_id,
            tool_name=tool_name,
        )

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
        stdout_meta: dict | None = None,
        stderr_meta: dict | None = None,
    ) -> str:
        envelope: dict = {
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
        }
        apply_stream_meta_to_envelope(envelope, "stdout", stdout_meta)
        apply_stream_meta_to_envelope(envelope, "stderr", stderr_meta)
        if envelope.get("stdout_output_ref") or envelope.get("stderr_output_ref"):
            envelope["hint"] = (
                "stdout/stderr shown as start+end preview. "
                f"Page full captured stream via {READ_STORED_TOOL_OUTPUT_NAME} "
                "using stdout_output_ref / stderr_output_ref."
            )
        return json.dumps(envelope, ensure_ascii=False)

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
            cwd=cwd or (self._backend.default_cwd() or ""),
            exit_code=None,
            timed_out=False,
            truncated=False,
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
        max_output_chars: int | None = None,
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
                truncated=False,
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
                truncated=False,
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
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=False,
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "max_output_chars"},
            )
        max_output_chars = clamp_max_output_chars(max_output_chars, self._config_dict)

        if cwd_used is not None:
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
            if not self._backend.is_dir(cwd_used):
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

        env_sanitized: dict[str, str] = {}
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return self._result(
                    ok=False,
                    command=command,
                    cwd=cwd_used or "",
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
                        cwd=cwd_used or "",
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
                        cwd=cwd_used or "",
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
                        cwd=cwd_used or "",
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
            # Ensure string keys and values for the backend's exec()
            for k, v in env_from_pairs.items():
                env_sanitized[str(k)] = "" if v is None else str(v)

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
                truncated=False,
                stdout="",
                stderr="",
                error={"type": "execution_failed", "message": err},
            )

        stdout_meta = self._process_stream(
            res.stdout or "",
            max_output_chars,
            hit_capture_cap=getattr(res, "stdout_hit_capture_cap", False),
            state=self._state,
            call_id=self._call_id,
            tool_name=ExecuteCommand.registered_tool_name(),
        )
        stderr_meta = self._process_stream(
            res.stderr or "",
            max_output_chars,
            hit_capture_cap=getattr(res, "stderr_hit_capture_cap", False),
            state=self._state,
            call_id=self._call_id,
            tool_name=ExecuteCommand.registered_tool_name(),
        )
        truncated = stdout_meta["truncated"] or stderr_meta["truncated"]

        if res.timed_out:
            return self._result(
                ok=False,
                command=command,
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=True,
                truncated=truncated,
                stdout=stdout_meta["text"],
                stderr=stderr_meta["text"],
                error={"type": "timeout", "message": f"Command timed out after {timeout_seconds} seconds"},
                stdout_meta=stdout_meta,
                stderr_meta=stderr_meta,
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
            truncated=truncated,
            stdout=stdout_meta["text"],
            stderr=stderr_meta["text"],
            error=error,
            stdout_meta=stdout_meta,
            stderr_meta=stderr_meta,
        )
