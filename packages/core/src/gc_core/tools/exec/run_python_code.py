import os
import re
from pathlib import Path
from typing import Any, Dict, Optional

from gc_core.backends.async_protocol import aexec_port
from gc_core.config_modules.agent import (
    exec_output_preview_edge_chars_from_config_dict,
    exec_timeout_from_config_dict,
)
from gc_core.constants.tool_names import EXEC_PYTHON_NAME
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import (
    effective_command_cwd,
    moderate_command_execution,
)
from gc_core.security.path_policy import resolve_tool_context
from gc_core.tools.exec.exec_envelope import (
    as_captured_stream,
    build_exec_envelope,
)
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


def _cwd_invalid_error(
    cwd_used: Optional[str], allowed_write_paths: list
) -> dict:
    return {
        "type": "cwd_invalid",
        "message": f"Working directory is not an existing directory: {cwd_used}",
        "cwd": cwd_used,
        "allowed_write_paths": allowed_write_paths,
        "hint": (
            "Pass cwd pointing to an existing directory. If allowed_write_paths is present above, "
            "use one of those paths — they are the writable locations allowed for this agent run."
        ),
    }


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_env_vars(
    env_vars,
) -> "tuple[Optional[Dict[str, str]], Optional[dict]]":
    """Список {'name','value'} → dict (None, если не задан); (None, error) при невалидной записи."""
    if env_vars is None:
        return None, None
    if not isinstance(env_vars, list):
        return None, {
            "type": "invalid_argument",
            "field": "env_vars",
            "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
        }
    parsed: Dict[str, str] = {}
    for idx, item in enumerate(env_vars):
        if not isinstance(item, dict):
            return None, {
                "type": "invalid_argument",
                "field": f"env_vars[{idx}]",
                "message": "Each env_vars item must be an object: {'name': 'KEY', 'value': 'VALUE'}",
            }
        name = item.get("name")
        # Только идентификатор: у remote-бэкендов имя попадает в `export K=V`
        # той стороны, и `X=1; rm -rf /; Y` в имени — команда мимо модерации.
        if not isinstance(name, str) or not _ENV_NAME_RE.fullmatch(
            name.strip()
        ):
            return None, {
                "type": "invalid_argument",
                "field": f"env_vars[{idx}].name",
                "message": "env_vars name must be a shell identifier: [A-Za-z_][A-Za-z0-9_]*",
            }
        value = item.get("value")
        if not isinstance(value, str):
            return None, {
                "type": "invalid_argument",
                "field": f"env_vars[{idx}].value",
            }
        parsed[name.strip()] = value
    return parsed, None


class RunPythonCode(AbstractSimpleTool):
    @classmethod
    def tool_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        timeout_default = exec_timeout_from_config_dict(config_dict)
        description = {
            "type": "function",
            "name": "exec_python",
            "description": (
                "Run Python code synchronously in a subprocess. Returns JSON with ok, return_code, stdout, stderr."
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
                    "timeout": {
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
                "additionalProperties": False,
            },
            "strict": True,
        }
        return description

    @classmethod
    def prompt_description(cls, config_dict: Optional[Dict[str, Any]] = None):
        timeout_default = exec_timeout_from_config_dict(config_dict)
        description = (
            """Tool name: exec_python
Tool description: Run Python in a subprocess; returns JSON with return_code, stdout, stderr.
Input schema: {"type": "object", "properties": {"code": {"type": "string", "description": "Python code to execute."}, "timeout": {"type": "integer", "description": "Execution timeout in seconds.", "default": """
            + str(timeout_default)
            + """}, "cwd": {"type":"string","description":"Working directory (optional)"}, "env_vars": {"type":"array","description":"Env overrides as [{'name','value'}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}} , "required": ["code"], "additionalProperties": false}

Usage:
- Use this tool to execute small, self-contained Python scripts that do not require interaction.
- Code is executed with the current Python interpreter reading from stdin (no temp .py file).
- Default working directory is the agent's project/workspace root; relative paths resolve there. PYTHONPATH is cleared to avoid unintended imports from the project.
- If you need to create files and the default directory is read-only or invalid, pass cwd explicitly. If a tool error includes allowed_write_paths, choose one of those directories as cwd.
- Do NOT use this tool for long-running tasks, interactive input, or accessing external systems in an unsafe way.
- If execution exceeds the timeout, the process is terminated and an error message is returned.
"""
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
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(
                self._config_dict
            ),
            error=error,
        )

    def _command_moderation_blocked_result(
        self,
        error: Dict[str, Any],
        code: str,
        timeout: Optional[int] = None,
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
    async def acall(
        self,
        code: str,
        timeout: Optional[int] = None,
        cwd: Optional[str] = None,
        env_vars: Optional[list[dict]] = None,
        **kwargs,
    ) -> str:
        """Нативный вход: процесс через ``asyncio.create_subprocess_exec``.

        Ровно та же причина, что у ``exec_command``: слот общего пула держался
        весь таймаут запуска. Гварды навешены и здесь — иначе асинхронный путь
        обошёл бы политику. ``aexec_port`` вместо прямого ``backend.arun_python``
        оставляет рабочими бэкенды без нативного запуска (harbor, TUI).
        """
        early, plan = await self._plan(code, timeout, cwd, env_vars, **kwargs)
        if early is not None:
            return early
        try:
            res = await aexec_port(self._backend).arun_python(
                plan["code"],
                cwd=plan["cwd"],
                timeout=plan["timeout"],
                env=plan["env"],
            )
        except Exception as exc:
            return self._exec_failed(exc)
        return self._finish(res, plan)

    def _exec_failed(self, exc: Exception) -> str:
        """Отказ запуска — общий для обоих входов."""
        return self._result(
            ok=False,
            return_code=None,
            timed_out=False,
            stdout="",
            stderr="",
            error={"type": "execution_failed", "message": str(exc)},
        )

    async def _resolve_cwd(
        self, cwd: Optional[str], kwargs: dict
    ) -> "tuple[Optional[str], Optional[dict]]":
        """Рабочий каталог запуска или ошибка cwd_invalid (с подсказкой, куда можно писать)."""
        if self._backend.is_sandbox:
            cwd_used = cwd  # None → container default workdir
            if cwd_used is not None and not await self._backend.is_dir(
                cwd_used
            ):
                return cwd_used, _cwd_invalid_error(cwd_used, [])
            return cwd_used, None
        security_context = resolve_tool_context(self, **kwargs)
        cwd_used = effective_command_cwd(
            EXEC_PYTHON_NAME, {"cwd": cwd}, security_context
        )
        if Path(cwd_used).is_dir():
            return cwd_used, None
        launch_abs = os.path.abspath(str(security_context.launch_root))
        candidate_roots = _dedupe_abs_paths_ordered(
            [
                launch_abs,
                *[
                    os.path.abspath(str(extra))
                    for extra in (
                        security_context.write_guard.extra_paths or []
                    )
                ],
            ]
        )
        allowed_write_paths = [
            p
            for p in candidate_roots
            if Path(p).is_dir() and directory_allows_probe_write(p)
        ]
        return cwd_used, _cwd_invalid_error(cwd_used, allowed_write_paths)

    async def _plan(
        self,
        code: str,
        timeout: Optional[int],
        cwd: Optional[str],
        env_vars: Optional[list[dict]],
        **kwargs,
    ) -> "tuple[str | None, dict]":
        """Разобрать и проверить аргументы. Общее для обоих входов."""

        def reject(error: dict) -> "tuple[str, dict]":
            return self._result(
                ok=False,
                return_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error=error,
            ), {}

        if not isinstance(code, str):
            return reject({"type": "invalid_argument", "field": "code"})
        if not code.strip():
            return reject(
                {
                    "type": "invalid_argument",
                    "field": "code",
                    "message": "Code is empty",
                }
            )

        effective_timeout = (
            timeout
            if timeout is not None
            else exec_timeout_from_config_dict(self._config_dict)
        )
        if not isinstance(effective_timeout, int) or effective_timeout <= 0:
            return reject({"type": "invalid_argument", "field": "timeout"})

        if cwd is not None and (not isinstance(cwd, str) or not cwd.strip()):
            return reject({"type": "invalid_argument", "field": "cwd"})

        cwd_used, cwd_error = await self._resolve_cwd(cwd, kwargs)
        if cwd_error is not None:
            return reject(cwd_error)

        env_map, env_error = _parse_env_vars(env_vars)
        if env_error is not None:
            return reject(env_error)

        return None, {
            "code": code,
            "cwd": cwd_used,
            "timeout": effective_timeout,
            "env": env_map,
        }

    def _finish(self, res: "Any", plan: dict) -> str:
        """Разобрать результат запуска. Общий для обоих входов."""
        effective_timeout = plan["timeout"]

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
                error={
                    "type": "timeout",
                    "message": f"Execution timed out after {effective_timeout} seconds",
                },
            )

        return_code = res.return_code
        ok = return_code == 0
        error = (
            None
            if ok
            else {
                "type": "non_zero_exit",
                "message": f"Process exited with status {return_code}",
            }
        )
        return self._result(
            ok=ok,
            return_code=return_code,
            timed_out=False,
            stdout=stdout_stream,
            stderr=stderr_stream,
            error=error,
        )
