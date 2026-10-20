import re
from typing import Any

from gc_core.backends.async_protocol import aexec_port
from gc_core.backends.runtime_probe import (
    RuntimeUnavailable,
    _probe_failure_message,
)
from gc_core.config_modules.agent import (
    DEFAULT_EXEC_TIMEOUT_SEC,
    exec_output_preview_edge_chars_from_config_dict,
    exec_timeout_from_config_dict,
)
from gc_core.constants.tool_names import EXEC_COMMAND_NAME
from gc_core.message_policy.user_stop import cancelled_by_user_output
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import moderate_command_execution
from gc_core.tools.exec.exec_envelope import (
    as_captured_stream,
    build_exec_envelope,
)
from gc_core.tools.simple_tool_base import AbstractSimpleTool
from gc_core.user_stop import UserStopInterrupt

_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _parse_env_vars(env_vars) -> "tuple[dict[str, str], dict | None]":
    """Список {'name','value'} → dict; (пусто, error) при первой невалидной записи."""
    if env_vars is None:
        return {}, None
    if not isinstance(env_vars, list):
        return {}, {
            "type": "invalid_argument",
            "field": "env_vars",
            "message": "env_vars must be a list of key/value pairs: [{'name': 'KEY', 'value': 'VALUE'}]",
        }
    env_from_pairs: dict[str, str] = {}
    for idx, item in enumerate(env_vars):
        if not isinstance(item, dict):
            return {}, {
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
            return {}, {
                "type": "invalid_argument",
                "field": f"env_vars[{idx}].name",
                "message": "env_vars name must be a shell identifier: [A-Za-z_][A-Za-z0-9_]*",
            }
        value = item.get("value")
        if not isinstance(value, str):
            return {}, {
                "type": "invalid_argument",
                "field": f"env_vars[{idx}].value",
                "message": "env_vars value must be a string",
            }
        env_from_pairs[name.strip()] = value
    return env_from_pairs, None


class ExecuteCommand(AbstractSimpleTool):
    """
    Tool to execute a CLI command on the system and return its output.
    """

    # Fallback when settings have no exec_timeout_sec (tests / partial wiring).
    timeout = DEFAULT_EXEC_TIMEOUT_SEC

    @classmethod
    def tool_description(cls, settings: Any = None):
        timeout_default = exec_timeout_from_config_dict(settings)
        description = {
            "type": "function",
            "name": "exec_command",
            "description": (
                "Run a shell command synchronously. Returns JSON with ok, exit_code, stdout, stderr. "
                "The `timeout` argument is the deadline that kills the process; a `timeout` prefix inside the command does not extend it."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {
                        "type": "string",
                        "description": "The command-line command to execute. Must be valid for the operating system and properly quoted, especially when paths contain spaces. Avoid interactive or destructive commands unless absolutely necessary.",
                    },
                    "timeout": {
                        "type": "integer",
                        "description": (
                            "Deadline for this call, in seconds. When it elapses the process is killed. "
                            f"Omit it and the default {timeout_default} applies (agent.exec_timeout_sec). "
                            "A `timeout` prefix inside `command` does not extend this deadline. "
                            "If the command needs longer, pass a larger number here."
                        ),
                        "default": timeout_default,
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
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Start the command detached and return without waiting for it to finish. "
                            "Use for long-lived processes: dev servers, watchers, `npm run dev`, `uvicorn`. "
                            "It keeps running after the agent exits. Stdout and stderr are not captured. "
                            "A process still running is reported by pid. One that exits at once is an error "
                            "with its exit code and no pid. Check a server by probing it (e.g. request its port). "
                            "To see a startup error, run that command once in the foreground with a small "
                            "`timeout`: the failure is in stdout/stderr, and a process that stays up is killed "
                            "when the timeout elapses. That run does not leave it running — start the service "
                            "again with this flag. Without this flag the command does not outlive the call. "
                            "Appending `&` does not change that: on timeout or cancel the process group is killed, "
                            "and the process dies with the agent. Output printed before that kill is in the reply."
                        ),
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            "strict": True,
        }
        return description

    @classmethod
    def prompt_description(cls, settings: Any = None):
        timeout_default = exec_timeout_from_config_dict(settings)
        description = (
            """Tool name: exec_command
Tool description: Run a shell command synchronously; returns JSON with exit_code, stdout, stderr.
Input schema: {"type": "object", "properties": {"command": {"type": "string", "description": "The CLI command to execute."}, "timeout": {"type": "integer", "description": "Execution timeout in seconds.", "default": """
            + str(timeout_default)
            + """}, "cwd": {"type": "string", "description": "Optional working directory (absolute or relative)."}, "env_vars": {"type":"array","description":"Optional env overrides as [{name,value}]","items":{"type":"object","properties":{"name":{"type":"string"},"value":{"type":"string"}},"required":["name","value"],"additionalProperties":false}}, "run_in_background": {"type": "boolean", "description": "Start detached and return at once; for long-lived commands. Output is not captured. Pid if the process is still running."}}, "required": ["command"], "additionalProperties": false}

Usage:
- Use this tool to run shell commands such as listing directories (`ls`, `dir`), checking versions (`python --version`), or running scripts.
- Always quote arguments properly to avoid shell injection risks (the tool uses safe execution via subprocess).
- The command is executed in the current working directory.
- Always returns both stdout and stderr; check ok / exit_code for success or failure.
- Be cautious with destructive commands (e.g., `rm`, `del`, `rmdir`).
- Long-lived commands (dev servers, watchers, `npm run dev`, `uvicorn`) MUST be started
  with `run_in_background: true`. They then keep running after this call returns and
  after the agent itself exits. Stdout and stderr are not captured. A process still
  running is reported by pid; one that exits at once is an error with its exit code
  and no pid. Check a server by probing it (e.g. request its port). To see a startup
  error, run that command once in the foreground with a small `timeout`: the failure
  is in stdout/stderr, and a process that stays up is killed when the timeout elapses.
  That run does not leave it running — start the service again with `run_in_background: true`.
  Without this flag the command does not outlive the call. Appending `&` does not change
  that: on timeout or cancel the process group is killed, and the process dies with the
  agent. Output printed before that kill is in the reply.
- On timeout the command is stopped and its work is lost. If it simply needs longer,
  re-run it with a bigger `timeout`, or start it with `run_in_background: true`.
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
3. Read and search file contents with the filesystem tools (`fs_read_file`, `fs_find_in_files`, `fs_find_files_by_name`, `fs_list_files`, `fs_search_and_replace`, …): they are permission-checked and return structured results. Do NOT use shell `cat`/`head`/`tail`/`grep`/`rg`/`find` to read or search file contents — use those tools instead. Shell is right for everything else.
4. Try to maintain your current working directory throughout the session by using absolute paths and avoiding usage of `cd`. You may use `cd` if the User explicitly requests it.
    <good-example>
    pytest /foo/bar/tests
    </good-example>
    <bad-example>
    cd /foo/bar && pytest tests
    </bad-example>
"""
        )
        return description

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
        """Envelope с front-loaded preview; ``truncated`` только если поток обрезан."""
        return build_exec_envelope(
            tool=ExecuteCommand.registered_tool_name(),
            ok=ok,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=as_captured_stream(stdout),
            stderr=as_captured_stream(stderr),
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(
                self._settings_source
            ),
            error=error,
            head_fields={"command": command, "cwd": cwd},
        )

    def _command_moderation_blocked_result(
        self,
        error: dict,
        command: str,
        timeout: int | None = None,
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

    async def _cwd_error(self, cwd_used) -> "dict | None":
        if cwd_used is None:
            return None
        if not isinstance(cwd_used, str) or not cwd_used.strip():
            return {"type": "invalid_argument", "field": "cwd"}
        # Прогретый cwd уже известен: Harbor — WORKDIR из `aprime_default_cwd`,
        # RemoteShell — заданный cwd или тот же `pwd`. Повторный `is_dir` перед
        # каждой командой — лишний round-trip. Чужой явный cwd проверяется.
        # Сбой транспорта — не «нет каталога»: иначе модель чинит cwd вместо
        # того, чтобы сдать отчёт через turn_finish.
        default_cwd = getattr(self._backend, "default_cwd", None)
        default = default_cwd() if callable(default_cwd) else None
        if isinstance(default, str) and cwd_used == default:
            return None
        try:
            is_directory = await self._backend.is_dir(cwd_used)
        except RuntimeUnavailable as exc:
            return {"type": "execution_failed", "message": str(exc)}
        except OSError as exc:
            if not getattr(self._backend, "is_sandbox", False):
                raise
            # `is_dir` здесь — ответ 0/1. Исключение значит, что ответа нет:
            # та же ситуация, что и непройденная проба суммы. Текст не разбираем.
            err = str(exc).replace("\n", " ").strip()
            return {
                "type": "execution_failed",
                "message": _probe_failure_message("", err),
            }
        if not is_directory:
            return {
                "type": "invalid_argument",
                "field": "cwd",
                "message": "Not a directory",
            }
        return None

    async def _plan(
        self,
        command: str,
        timeout: int | None,
        cwd: str | None,
        env_vars: list[dict] | None,
    ) -> "tuple[str | None, dict]":
        """Разобрать и проверить аргументы. Общее для обоих входов.

        Возвращает ``(ранний_ответ, план)``: ранний ответ — готовый JSON отказа
        по невалидному аргументу, план — то, с чем идти в бэкенд. Вынесено
        затем, чтобы синхронный и асинхронный входы не могли разойтись в
        проверках: у них различается ТОЛЬКО строка запуска.
        """
        cwd_used = cwd if cwd is not None else self._backend.default_cwd()

        def reject(
            error: dict, *, cmd: str = command, cwd_val: "str | None" = None
        ) -> "tuple[str, dict]":
            return self._result(
                ok=False,
                command=cmd,
                cwd=(cwd_used or "") if cwd_val is None else cwd_val,
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error=error,
            ), {}

        # Input validation
        if not isinstance(command, str):
            return reject(
                {"type": "invalid_argument", "field": "command"},
                cmd=str(command),
            )
        if not command.strip():
            return reject(
                {
                    "type": "invalid_argument",
                    "field": "command",
                    "message": "Empty command",
                }
            )

        if timeout is None:
            timeout = exec_timeout_from_config_dict(self._settings_source)
        if not isinstance(timeout, int) or timeout <= 0:
            return reject({"type": "invalid_argument", "field": "timeout"})

        cwd_error = await self._cwd_error(cwd_used)
        if cwd_error is not None:
            return reject(cwd_error, cwd_val=str(cwd_used))

        env_sanitized, env_error = _parse_env_vars(env_vars)
        if env_error is not None:
            return reject(env_error)

        return None, {
            "command": command,
            "cwd": cwd_used,
            "timeout": timeout,
            "env": env_sanitized or None,
        }

    @enforce_command_guard(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    @moderate_command_execution(
        payload_arg="command", tool_name=EXEC_COMMAND_NAME
    )
    async def acall(
        self,
        command: str,
        timeout: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        run_in_background: bool = False,
        **kwargs,
    ) -> str:
        """Нативный вход: процесс через ``asyncio.create_subprocess_shell``.

        Прежде тул целиком уезжал в ``asyncio.to_thread``, и слот общего пула
        держался ВЕСЬ таймаут — по умолчанию 120 с. На одиночном вызове это
        почти незаметно, на параллельных — нет: 20 команд шли 4.09 с через пул
        против 2.10 с нативно.

        Гварды навешены и здесь — иначе асинхронный путь обошёл бы политику
        безопасности.

        ``aexec_port`` вместо прямого ``backend.aexec``: у бэкендов без
        нативного ``aexec`` адаптер завернёт вызов по-старому — совместимость
        цела.

        Разбор аргументов общий с синхронным входом: он весь выше по функции
        ``call``, поэтому здесь мы её и зовём — но в потоке, а не на loop, и
        только ради разбора. Сам запуск ждётся через ``await``.
        """
        early, plan = await self._plan(command, timeout, cwd, env_vars)
        if early is not None:
            return early
        if run_in_background and not getattr(
            self._backend, "supports_background", False
        ):
            return await self._abackground(plan)
        if run_in_background:
            return await self._abackground(plan)
        try:
            res = await aexec_port(self._backend).aexec(
                plan["command"],
                cwd=plan["cwd"],
                timeout=plan["timeout"],
                env=plan["env"],
            )
        except RuntimeUnavailable as exc:
            return self._runtime_unavailable(plan, exc)
        except UserStopInterrupt:
            raise
        except Exception as exc:
            return self._exec_failed(plan, exc)
        return self._finish(res, plan)

    async def _abackground(self, plan: dict) -> str:
        """Запустить и НЕ ждать: отдать pid.

        Два случая разведены намеренно. Команда переднего плана живёт секунды,
        её вывод нужен сейчас, и переживать вызов она не должна. Долгоживущая —
        наоборот: ждать её нельзя, и никакой связи с нами у неё быть не должно.
        Труба такой связью была: пропадал читатель — и сервис, поднятый агентом,
        погибал вместе с ним. Поэтому вывод фона уходит в нулевое устройство, и
        процесс переживает и конец хода, и выход самого агента.

        Лога нет намеренно. Прежде вывод шёл в файл, и оба варианта упирались в
        гарды: `log_path` называла модель — тул становился примитивом записи, и
        путь гнали через гард записи с вопросами и откатами; свой лог лежал в
        `.gc/tmp`, а весь `.gc` закрыт на чтение — модель не могла прочитать то,
        что ей велели читать. Поэтому ответ прямо говорит, что вывода не будет.
        """
        cwd_used = plan["cwd"]

        # Отказ ДО запуска: если бэкенд не умеет фон, процесса не будет.
        # Раньше метода на порту не было, и вызов через обёртку TUI падал
        # `AttributeError` — в единственном месте, где фоновый режим и нужен.
        if not getattr(self._backend, "supports_background", False):
            return self._result(
                ok=False,
                command=plan["command"],
                cwd=cwd_used or "",
                exit_code=None,
                timed_out=False,
                stdout="",
                stderr="",
                error={
                    "type": "background_not_supported",
                    "message": (
                        "This backend cannot start background commands. Run the command in the foreground instead."
                    ),
                },
            )

        try:
            started = await self._backend.aspawn_background(
                plan["command"],
                cwd=cwd_used,
                env=plan["env"],
            )
        except RuntimeUnavailable as exc:
            return self._runtime_unavailable(plan, exc)
        except Exception as exc:
            from gc_core.execution.resource_scope import (
                resource_capture_active,
            )

            if resource_capture_active():
                from gc_core.execution.contracts import ToolOutcomeUnknown

                raise ToolOutcomeUnknown(
                    "Background start did not return a reliable receipt"
                ) from exc
            return self._exec_failed(plan, exc)

        # Команда могла не прожить и мгновения (бэкенд, который это проверяет,
        # отдаёт её код). Упавшая — не «запущена»: иначе опечатка выглядела бы
        # сервисом, и модель шла бы проверять его порт.
        exit_code = started.get("exit_code")
        if exit_code not in (None, 0):
            return self._result(
                ok=False,
                command=plan["command"],
                cwd=cwd_used or "",
                exit_code=exit_code,
                timed_out=False,
                stdout="",
                stderr="",
                error={
                    "type": "background_exited",
                    "message": (
                        f"The command exited right after start with exit code {exit_code}: "
                        "nothing is running in the background. Its output is not captured — "
                        "run it in the foreground with a short `timeout` to see the error."
                    ),
                },
            )
        if exit_code is None and isinstance(started.get("pid"), int):
            from gc_core.execution.resource_scope import (
                record_background_process,
            )

            record_background_process(
                started["pid"], type(self._backend).__name__
            )
        background = {"pid": started.get("pid"), "output": "not captured"}
        if exit_code == 0:
            background["note"] = (
                "The command exited with code 0 right after start. Unless it detached "
                "a process of its own (a daemon), nothing of it is running now."
            )
        return build_exec_envelope(
            tool=ExecuteCommand.registered_tool_name(),
            ok=True,
            exit_code=exit_code,
            timed_out=False,
            stdout=as_captured_stream(""),
            stderr=as_captured_stream(""),
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(
                self._settings_source
            ),
            error=None,
            head_fields={
                "command": plan["command"],
                "cwd": cwd_used or "",
                "background": background,
            },
        )

    def _runtime_unavailable(self, plan: dict, exc: RuntimeUnavailable) -> str:
        """Бэкенд не дошёл до команды: проба суммы не вернулась."""
        return self._result(
            ok=False,
            command=plan["command"],
            cwd=plan["cwd"] or "",
            exit_code=-1 if exc.return_code is None else exc.return_code,
            timed_out=exc.timed_out,
            stdout=exc.stdout,
            stderr=exc.stderr,
            error={"type": "execution_failed", "message": str(exc)},
        )

    def _exec_failed(self, plan: dict, exc: Exception) -> str:
        """Отказ запуска — общий для обоих входов."""
        err = str(exc).replace("\n", " ").strip()
        return self._result(
            ok=False,
            command=plan["command"],
            cwd=plan["cwd"] or "",
            exit_code=-1,
            timed_out=False,
            stdout="",
            stderr="",
            error={"type": "execution_failed", "message": err},
        )

    def _finish(self, res: "Any", plan: dict) -> str:
        """Разобрать результат запуска. Общий для обоих входов."""
        command = plan["command"]
        cwd_used = plan["cwd"]
        timeout = plan["timeout"]

        # getattr, а не атрибут: backend вправе вернуть свой результат-подобный
        # объект без структуры потока — тогда падаем обратно на плоский текст,
        # а не роняем тулу AttributeError уже после исполнения команды.
        stdout_stream = as_captured_stream(
            getattr(res, "stdout_stream", None) or getattr(res, "stdout", "")
        )
        stderr_stream = as_captured_stream(
            getattr(res, "stderr_stream", None) or getattr(res, "stderr", "")
        )

        if getattr(res, "stopped_by_user", False):
            return cancelled_by_user_output(
                ExecuteCommand.registered_tool_name(),
                meta={
                    "stdout": getattr(res, "stdout", "") or "",
                    "stderr": getattr(res, "stderr", "") or "",
                },
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
                error={
                    "type": "timeout",
                    "message": (
                        f"Command timed out after {timeout} seconds. "
                        "This deadline is the `timeout` argument of this tool. "
                        "A `timeout` prefix inside the command does not extend it. "
                        "Call again and pass a larger `timeout`."
                    ),
                },
            )

        exit_code = res.return_code
        ok = exit_code == 0
        error = (
            None
            if ok
            else {
                "type": "non_zero_exit",
                "message": f"Command exited with status {exit_code}",
            }
        )
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
