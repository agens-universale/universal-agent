from gc_core.backends.async_protocol import aexec_port
from gc_core.tools.simple_tool_base import AbstractSimpleTool
import json
import os
import time
from typing import Any, Dict, Optional

from gc_core.config_modules.agent import DEFAULT_EXEC_TIMEOUT_SEC, exec_timeout_from_config_dict
from gc_core.config_modules.agent import exec_output_preview_edge_chars_from_config_dict
from gc_core.constants.tool_names import EXEC_COMMAND_NAME
from gc_core.security.command_guard import enforce_command_guard
from gc_core.security.command_moderation import moderate_command_execution
from gc_core.security.path_policy import enforce_write_guard
from gc_core.tools.exec.exec_envelope import as_captured_stream, build_exec_envelope


def _open_unique_log(directory: str) -> "tuple[int, str]":
    """Открыть СВОЙ лог, а не чужой: имя уникально, создание атомарно.

    Имя было `bg-<время до секунды>.log`, и два фоновых запуска в одной пачке
    вызовов — скажем, фронтенд и бэкенд — получали один путь. Их вывод
    перемешивался в одном файле, хотя обеим моделям сказано «читай свой лог».

    `O_EXCL` здесь важнее случайного суффикса: он и делает «занять имя»
    атомарным. `mkstemp` не подошёл бы — он не умеет `O_APPEND`, а дозапись
    нужна, потому что дескриптор наследует потомок.
    """
    import errno
    import secrets

    os.makedirs(directory, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_APPEND
    for _ in range(8):
        path = os.path.join(directory, f"bg-{stamp}-{secrets.token_hex(4)}.log")
        try:
            return os.open(path, flags, 0o600), path
        except FileExistsError:
            continue
    raise OSError(errno.EEXIST, "no free background log name", directory)


def _guard_refusal_as_value(error: dict) -> dict:
    """Отказ гарда — ЗНАЧЕНИЕ, а не готовый ответ тула.

    `enforce_write_guard` по умолчанию собирает из ошибки envelope тула и
    возвращает его наружу. Для лога это неверно: отказ в логе НЕ отменяет
    запуск команды, он лишь означает «вывод девать некуда». Поэтому забираем
    словарь ошибки себе и вкладываем причину в `note` фонового ответа.
    """
    return error


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
                    "run_in_background": {
                        "type": "boolean",
                        "description": (
                            "Start the command detached and return immediately, without waiting "
                            "for it to finish. Use this for anything long-lived: dev servers, "
                            "watchers, `npm run dev`, `uvicorn`. The reply carries the pid and the "
                            "log path instead of the output. A command started WITHOUT this flag "
                            "does not outlive the call — appending `&` to it is not enough, "
                            "because its output pipes close when the call returns."
                        ),
                    },
                    "log_path": {
                        "type": "string",
                        "description": (
                            "Where to write the background command's output. Defaults to a file "
                            "under .gc/tmp/ inside the project. Only used together with "
                            "run_in_background. A path outside the project requires the user's "
                            "confirmation; if writing is refused the command still starts, but its "
                            "output is discarded — the reply says so explicitly."
                        ),
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

Usage:
- Use this tool to run shell commands such as listing directories (`ls`, `dir`), checking versions (`python --version`), or running scripts.
- Always quote arguments properly to avoid shell injection risks (the tool uses safe execution via subprocess).
- The command is executed in the current working directory.
- Always returns both stdout and stderr; check ok / exit_code for success or failure.
- Be cautious with destructive commands (e.g., `rm`, `del`, `rmdir`).
- Long-lived commands (dev servers, watchers, `npm run dev`, `uvicorn`) MUST be started
  with `run_in_background: true`. They then keep running after this call returns and
  after the agent itself exits, and you read their output from the returned log path.
  Appending `&` to a normal foreground command is NOT enough: such a command is killed
  on timeout and its output pipes close when the call returns.
- On timeout the command is stopped and its work is lost. If it simply needs longer,
  re-run it with a bigger `timeout_seconds`, or start it with `run_in_background: true`.
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
''')
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
        """Envelope с front-loaded preview; ``truncated`` выводится из потоков."""
        return build_exec_envelope(
            tool=ExecuteCommand.registered_tool_name(),
            ok=ok,
            exit_code=exit_code,
            timed_out=timed_out,
            stdout=as_captured_stream(stdout),
            stderr=as_captured_stream(stderr),
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

    async def _plan(
        self,
        command: str,
        timeout_seconds: int | None,
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
            ), {}

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
            ), {}

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
            ), {}


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
                ), {}
            if not await self._backend.is_dir(cwd_used):
                return self._result(
                    ok=False,
                    command=command,
                    cwd=cwd_used,
                    exit_code=None,
                    timed_out=False,
                    stdout="",
                    stderr="",
                    error={"type": "invalid_argument", "field": "cwd", "message": "Not a directory"},
                ), {}

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
                ), {}
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
                    ), {}
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
                    ), {}
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
                    ), {}
                env_from_pairs[name.strip()] = value
            env_sanitized = env_from_pairs

        return None, {
            "command": command,
            "cwd": cwd_used,
            "timeout": timeout_seconds,
            "env": env_sanitized or None,
        }

    @enforce_command_guard(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    @moderate_command_execution(payload_arg="command", tool_name=EXEC_COMMAND_NAME)
    async def acall(
        self,
        command: str,
        timeout_seconds: int | None = None,
        cwd: str | None = None,
        env_vars: list[dict] | None = None,
        run_in_background: bool = False,
        log_path: str | None = None,
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
        early, plan = await self._plan(command, timeout_seconds, cwd, env_vars)
        if early is not None:
            return early
        if run_in_background:
            return await self._abackground(plan, log_path)
        try:
            res = await aexec_port(self._backend).aexec(
                plan["command"], cwd=plan["cwd"], timeout=plan["timeout"],
                env=plan["env"],
            )
        except Exception as exc:
            return self._exec_failed(plan, exc)
        return self._finish(res, plan)

    async def _abackground(self, plan: dict, log_path: str | None) -> str:
        """Запустить и НЕ ждать: отдать pid и путь к логу.

        Два случая разведены намеренно. Команда переднего плана живёт секунды,
        её вывод нужен сейчас, и переживать вызов она не должна. Долгоживущая —
        наоборот: ждать её нельзя, а её вывод приходит часами, поэтому он идёт
        в ФАЙЛ, который не зависит от того, жив ли читатель. Ровно поэтому фон
        переживает и конец хода, и выход самого агента: файлу всё равно, кто
        его открыл.

        Лог открываем ЗДЕСЬ, а не в бэкенде: решение принимает гард записи, а
        он живёт на слое тулы. Бэкенд получает готовый дескриптор либо ничего.
        """
        from gc_core.config_modules.env_vars import project_gc_dir
        from gc_core.security.path_policy import resolve_tool_context

        cwd_used = plan["cwd"]
        log_fd: int | None = None
        note = ""

        # Отказ ДО всего остального: если бэкенд не умеет фон, не создаём ни
        # лога, ни процесса. Раньше метода на порту не было, и вызов через
        # обёртку TUI падал `AttributeError` — в единственном месте, где
        # фоновый режим и нужен.
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
                        "This backend cannot start background commands. "
                        "Run the command in the foreground instead."
                    ),
                },
            )

        try:
            context = resolve_tool_context(self)
        except Exception as exc:                      # контекст не собрался
            return self._exec_failed(plan, exc)

        if log_path:
            # Путь называет МОДЕЛЬ, значит он обязан пройти ровно тот же гард,
            # что и запись файловым тулом: периметр — лишь половина политики,
            # защищённые пути (AGENTS.md — спросить, `.gc` и конфиг — запретить)
            # живут только в `enforce_write_guard`. Без этого фоновый лог был
            # примитивом записи в файл, который агент читает как свои правила.
            target = os.path.abspath(
                os.path.join(cwd_used or context.launch_root, log_path)
            )
            refusal = await self._alog_write_refusal(path=target)
            if refusal is None:
                allowed = target
            else:
                # Отказали в МЕСТЕ, а не в захвате вывода. Поэтому пишем в свой
                # служебный лог: он выбран нами, а не моделью, значит инъекции
                # взяться неоткуда, и модель получает то, что ей нужно, —
                # читаемый лог. Терять вывод целиком незачем.
                try:
                    log_fd, allowed = _open_unique_log(
                        str(project_gc_dir(context.launch_root) / "tmp")
                    )
                    note = (
                        f"requested log path refused ({refusal.get('type')}: "
                        f"{refusal.get('message')}); output goes to {allowed} instead"
                    )
                except OSError as exc:
                    allowed = None
                    note = (
                        f"requested log path refused, and the default log could not "
                        f"be opened ({exc.strerror or exc}); output is discarded"
                    )
        else:
            # Путь по умолчанию выбираем МЫ, а не модель, и якорим к КОРНЮ
            # ПРОЕКТА: иначе `exec` с `cwd=/tmp` заводил бы лог в
            # `/tmp/.gc/tmp/` — вне периметра и там, где его не найти.
            #
            # Гард сюда не применяется намеренно: весь `.gc` защищён как
            # операционный каталог, но этот путь не приходил от модели, значит
            # инъекции взяться неоткуда. Исключение закреплено тестом.
            try:
                log_fd, allowed = _open_unique_log(
                    str(project_gc_dir(context.launch_root) / "tmp")
                )
            except OSError as exc:
                allowed = None
                note = f"log not written ({exc.strerror or exc}); output discarded"

        # Только для пути, названного МОДЕЛЬЮ: путь по умолчанию уже открыт
        # выше, атомарно и под своим уникальным именем. Без этого условия мы
        # открывали бы его второй раз, теряя первый дескриптор.
        if allowed is not None and log_fd is None:
            try:
                os.makedirs(os.path.dirname(str(allowed)) or ".", exist_ok=True)
                log_fd = os.open(str(allowed), os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            except OSError as exc:
                # Молчаливый откат — худшее, что тут можно сделать: модель пойдёт
                # читать лог, которого нет, и начнёт строить догадки.
                allowed = None
                note = f"log not written ({exc.strerror or exc}); output discarded"

        try:
            started = await self._backend.aspawn_background(
                plan["command"], cwd=cwd_used, env=plan["env"], log_fd=log_fd,
            )
        except Exception as exc:
            return self._exec_failed(plan, exc)
        finally:
            if log_fd is not None:
                try:
                    os.close(log_fd)          # копия потомка своя
                except OSError:
                    pass

        background = {"pid": started.get("pid"), "log_path": str(allowed) if allowed else None}
        if note:
            background["note"] = note
        return build_exec_envelope(
            tool=ExecuteCommand.registered_tool_name(),
            ok=True,
            exit_code=None,
            timed_out=False,
            stdout=as_captured_stream(""),
            stderr=as_captured_stream(""),
            preview_edge_chars=exec_output_preview_edge_chars_from_config_dict(self._config_dict),
            error=None,
            head_fields={
                "command": plan["command"],
                "cwd": cwd_used or "",
                "background": background,
            },
        )

    @enforce_write_guard(path_arg="path", blocked_result_factory=_guard_refusal_as_value)
    async def _alog_write_refusal(self, path: str) -> "dict | None":
        """Спросить гард записи про путь лога — и ничего не делать.

        Тело пустое намеренно: вся работа в декораторе. Дойти сюда — значит
        «писать можно», и метод отдаёт ``None``. Если гард запретил, его
        `blocked_result_factory` возвращает СЛОВАРЬ ошибки вместо результата
        тула, и вызывающий читает причину.

        Так фоновый лог получает ровно ту же политику, что обычная запись
        файла: защищённые пути, периметр, аудит, память решений и ожидание
        человека в потоке. Прежняя самодельная проверка знала только периметр,
        и через `log_path` можно было дописать в AGENTS.md без вопроса.
        """
        return None

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
        timeout_seconds = plan["timeout"]

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
