import json
import os
from pathlib import Path
from typing import Any, Dict, Optional

from gc_core.config_modules.agent import exec_timeout_from_config_dict
from gc_core.config_modules.agent import exec_output_preview_edge_chars_from_config_dict
from gc_core.constants.tool_names import EXEC_PYTHON_NAME
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import effective_command_cwd, moderate_command_execution
from gc_core.security.path_policy import resolve_tool_context
from gc_core.tools.exec.exec_envelope import as_captured_stream, build_exec_envelope
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.utils.directory_write_access import directory_allows_probe_write


def _dedupe_abs_paths_ordered(paths: list[str]) -> list[str]:
    seen: set[str] = set()
    ordered: list[str] = []
    for raw in paths:
        abs_path = os.path.abspath(str(raw))
        if abs_path not in seen:
            seen.add(abs_path)
            ordered.append(abs_path)
    return ordered


class RunPythonCode(AbstractSimpleTool):
    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        timeout_default = exec_timeout_from_config_dict(config_dict)
        description = {
            "type": "function",
            "name": "exec_python",
            "description": (
                "Run Python code synchronously in a subprocess. "
                "Returns JSON with ok, return_code, stdout, stderr."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "code": {
                        "type": "string",
                        "description": (
                            "The full Python code to execute via the current Python interpreter "
                            "(fed on standard input; no temp script file)."
                        ),
                    },
                    "timeout_seconds": {
                        "type": "integer",
                        "description": (
                            f"Maximum execution time in seconds before the process is terminated. "
                            f"Default is {timeout_default} (agent.exec_timeout_sec)."
                        ),
                        "default": timeout_default,
                        "minimum": 1,
                    },
                    "cwd": {
                        "type": "string",
                        "description": (
                            "Working directory for the subprocess (absolute or relative to the host). "
                            "When unset, defaults to this agent's configured project/workspace root so relative paths "
                            "resolve under the project. If the default is missing, read-only, or unsuitable, set cwd "
                            "explicitly. When the tool returns an error with allowed_write_paths, use one of those "
                            "paths as cwd (they are the permitted writable locations for this run)."
                        ),
                    },
                    "env_vars": {
                        "type": "array",
                        "description": "Optional environment variables to add/override as [{'name','value'}] pairs. Values must be strings.",
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
                "required": ["code"],
                "additionalProperties": False
            },
            "strict": True
        }
        return description

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        timeout_default = exec_timeout_from_config_dict(config_dict)
        description = (
            '''Tool name: exec_python
Tool description: Run Python in a subprocess; returns JSON with return_code, stdout, stderr.
Input schema: {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to execute."}, "timeout_seconds": {"type": "integer", "description": "Execution timeout in seconds.", "default": '''
            + str(timeout_default)
            + '''}, "cwd": {"type":"string","description":"Working directory (optional)"}, "env_vars": {"type":"array","description":"Env overrides as [{'name','value'}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}} , "required": ["code"], "additionalProperties": false}

Usage:
- Use this tool to execute small, self-contained Python scripts that do not require interaction.
- Code is executed with the current Python interpreter reading from stdin (no temp .py file).
- Default working directory is the agent's project/workspace root; relative paths resolve there. PYTHONPATH is cleared to avoid unintended imports from the project.
- If you need to create files and the default directory is read-only or invalid, pass cwd explicitly. If a tool error includes allowed_write_paths, choose one of those directories as cwd.
- Do NOT use this tool for long-running tasks, interactive input, or accessing external systems in an unsafe way.
- If execution exceeds the timeout, the process is terminated and an error message is returned.
'''
        )
        return description

    def _result(
        self,
        *,
        ok: bool,
        return_code: Optional[int],
        timed_out: bool,
        stdout=None,
        stderr=None,
        error: Optional[Dict[str, Any]] = None,
    ) -> str:
        """Envelope с front-loaded preview; ``truncated`` только если поток обрезан."""
        return build_exec_envelope(
            tool=RunPythonCode.registered_tool_name(),
            ok=ok,
            exit_code=return_code,
            exit_code_key="return_code",
            timed_out=timed_out,
            stdout=as_captured_stream(stdout),
            stderr=as_captured_stream(stderr),
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(self._config_dict),
            error=error,
        )

    def _command_moderation_blocked_result(
        self,
        error: Dict[str, Any],
        code: str,
        timeout_seconds: Optional[int] = None,
        cwd: Optional[str] = None,
        env_vars: Optional[list[dict]] = None,
        **kwargs,
    ) -> str:
        return self._result(
            ok=False,
            return_code=None,
            timed_out=False,
            stdout="",
            stderr="",
            error=error,
        )

    @enforce_command_guard(payload_arg="code", tool_name=EXEC_PYTHON_NAME)
    @moderate_command_execution(payload_arg="code", tool_name=EXEC_PYTHON_NAME)
    def __call__(
        self,
        code: str,
        timeout_seconds: Optional[int] = None,
        cwd: Optional[str] = None,
        env_vars: Optional[list[dict]] = None,
        **kwargs,
    ) -> str:
        if not isinstance(code, str):
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "code"},
            )

        if not code.strip():
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "code", "message": "Code is empty"},
            )

        effective_timeout = (
            timeout_seconds
            if timeout_seconds is not None
            else exec_timeout_from_config_dict(self._config_dict)
        )
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "timeout_seconds"},
            )


        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "invalid_argument", "field": "cwd"},
            )

        if self._backend.is_sandbox:
            cwd_used = cwd  # None → container default workdir
            if cwd_used is not None and not self._backend.is_dir(cwd_used):
                return self._result(
                    ok=False,
                    return_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={
                        "type": "cwd_invalid",
                        "message": f"Working directory is not an existing directory: {cwd_used}",
                        "cwd": cwd_used,
                        "allowed_write_paths": [],
                        "hint": (
                            "Pass cwd pointing to an existing directory. If allowed_write_paths is present above, "
                            "use one of those paths — they are the writable locations allowed for this agent run."
                        ),
                    },
                )
        else:
            security_context = resolve_tool_context(self, **kwargs)
            cwd_used = effective_command_cwd(EXEC_PYTHON_NAME, {"cwd": cwd}, security_context)

            if not Path(cwd_used).is_dir():
                launch_abs = os.path.abspath(str(security_context.launch_root))
                candidate_roots = _dedupe_abs_paths_ordered(
                    [
                        launch_abs,
                        *[
                            os.path.abspath(str(extra))
                            for extra in (security_context.write_guard.extra_paths or [])
                        ],
                    ]
                )
                allowed_write_paths = [
                    p for p in candidate_roots if Path(p).is_dir() and directory_allows_probe_write(p)
                ]
                return self._result(
                    ok=False,
                    return_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={
                        "type": "cwd_invalid",
                        "message": f"Working directory is not an existing directory: {cwd_used}",
                        "cwd": cwd_used,
                        "allowed_write_paths": allowed_write_paths,
                        "hint": (
                            "Pass cwd pointing to an existing directory. If allowed_write_paths is present above, "
                            "use one of those paths — they are the writable locations allowed for this agent run."
                        ),
                    },
                )

        env_map: Optional[Dict[str, str]] = None
        if env_vars is not None:
            if not isinstance(env_vars, list):
                return self._result(
                    ok=False,
                    return_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={
                        "type": "invalid_argument",
                        "field": "env_vars",
                        "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
                    },
                )
            parsed: Dict[str, str] = {}
            for idx, item in enumerate(env_vars):
                if not isinstance(item, dict):
                    return self._result(
                        ok=False,
                        return_code=None,
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
                        return_code=None,
                        timed_out=False,
                        stdout="",
                        stderr="",
                        error={"type": "invalid_argument", "field": f"env_vars[{idx}].name"},
                    )
                value = item.get("value")
                if not isinstance(value, str):
                    return self._result(
                        ok=False,
                        return_code=None,
                        timed_out=False,
                        stdout="",
                        stderr="",
                        error={"type": "invalid_argument", "field": f"env_vars[{idx}].value"},
                    )
                parsed[name.strip()] = value
            env_map = parsed

        try:
            res = self._backend.run_python(
                code, cwd=cwd_used, timeout=effective_timeout, env=env_map
            )
        except Exception as e:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={"type": "execution_failed", "message": str(e)},
            )
        # getattr, а не атрибут: backend вправе вернуть свой результат-подобный
        # объект без структуры потока — тогда падаем обратно на плоский текст,
        # а не роняем тулу AttributeError уже после исполнения команды.
        stdout_stream = as_captured_stream(
            getattr(res, "stdout_stream", None) or getattr(res, "stdout", "")
        )
        stderr_stream = as_captured_stream(
            getattr(res, "stderr_stream", None) or getattr(res, "stderr", "")
        )

        if res.timed_out:
            return self._result(
                ok=False,
                return_code=None,
                timed_out=True,
                stdout=stdout_stream,
                stderr=stderr_stream,
                error={"type": "timeout", "message": f"Execution timed out after {effective_timeout} seconds"},
            )

        return_code = res.return_code
        ok = return_code == 0
        error = (
            None
            if ok
            else {"type": "non_zero_exit", "message": f"Process exited with status {return_code}"}
        )
        return self._result(
            ok=ok,
            return_code=return_code,
            timed_out=False,
            stdout=stdout_stream,
            stderr=stderr_stream,
            error=error,
        )
