"""Coroutine FsBackend over an async Harbor BaseEnvironment.

Harbor's environment is bound to the adapter's event loop. Every operation here is a
coroutine: on that loop it awaits the environment directly, from any other loop (the
agent's own ``asyncio.run``) it schedules the call there and awaits the result via
``wrap_future`` — no thread is blocked either way (see ``_aawait``).
File ops mirror deepagents' HarborSandbox: write/edit use native upload/download,
read/list use shell through exec (``PosixShellFsMixin``); здесь только транспорт.
"""

import asyncio
import contextlib
import re
import shlex
import tempfile
from pathlib import Path
from typing import Any, ContextManager, Optional

from gc_core.backends.capture import BoundedStreamAccumulator
from gc_core.backends.posix_shell_fs import PosixShellFsMixin
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    WALK_OUTPUT_MAX_CHARS,
    ExecResult,
    FsBackend,
    argv_to_posix_command,
    bound_exec_result,
)
from gc_core.backends.runtime_probe import (
    DEFAULT_RUNTIME_PROBE_TTL_SEC,
    RuntimeProbeCache,
)
from gc_core.backends.walk_util import FlatWalkMixin
from gc_core.user_stop import current_user_stop

# Встроенная политика ``harbor_exec_init: bashrc``. Неинтерактивный ``bash -c``
# иначе пропускает bashrc (``[ -z "$PS1" ] && return``). Потоки профиля
# глушатся, чтобы conda-хуки не попали в stdout/stderr команды; ``set +e``
# после source — чтобы ``set -e`` / ``return`` в rc не подменили код возврата
# пользовательской команды. Имя окружения не зашито.
# ``/etc/profile`` на Debian для root задаёт PATH заново и выбрасывает каталоги
# из ENV образа (``/opt/venv/bin``, ``/usr/local/go/bin``, cargo). Впереди
# остаются только каталоги, которых не было в сохранённом PATH (conda/rustup),
# в том порядке, в каком их поставили профиль и bashrc. Дальше сохранённый
# PATH в исходном порядке: системные каталоги профиля его не переставляют.
_BASHRC_EXEC_INIT = """\
PS1=${PS1-dummy}
_gc_saved_path=$PATH
. /etc/profile >/dev/null 2>&1 || true
[ -f "${HOME}/.bashrc" ] && . "${HOME}/.bashrc" >/dev/null 2>&1 || true
set +e
_gc_ifs=$IFS
IFS=:
case $- in *f*) _gc_noglob=1 ;; *) _gc_noglob=0; set -f ;; esac
_gc_new=
for _gc_dir in ${PATH}; do
  [ -z "${_gc_dir}" ] && continue
  case ":${_gc_saved_path}:" in
    *":${_gc_dir}:"*) continue ;;
  esac
  case ":${_gc_new}:" in
    *":${_gc_dir}:"*) continue ;;
  esac
  _gc_new="${_gc_new:+${_gc_new}:}${_gc_dir}"
done
if [ -n "${_gc_new}" ]; then
  PATH="${_gc_new}${_gc_saved_path:+:${_gc_saved_path}}"
else
  PATH="${_gc_saved_path}"
fi
[ "${_gc_noglob}" -eq 0 ] && set +f
IFS=$_gc_ifs
export PATH
unset _gc_saved_path _gc_ifs _gc_dir _gc_noglob _gc_new
"""


_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


def _env_assignment_prefix(env: Optional[dict]) -> str:
    if not env:
        return ""
    bad = [k for k in env if not _ENV_NAME_RE.fullmatch(str(k))]
    if bad:
        # Имя уходит в строку `env K=V` без кавычек: не идентификатор — команда.
        raise ValueError(f"invalid environment variable name(s): {bad}")
    assignments = " ".join(
        f"{k}={shlex.quote(str(v))}" for k, v in env.items()
    )
    return f"env {assignments} "


def build_harbor_shell_command(
    command: str,
    *,
    cwd: Optional[str] = None,
    env: Optional[dict] = None,
    exec_init: Optional[str] = None,
) -> str:
    """Строка, которую Harbor ``environment.exec`` обернёт в ``bash -c``.

    С инициализацией: ``cd cwd && env K=V bash -c <init+command>``. Без неё
    команда остаётся сырой. ``cd`` стоит снаружи ``env``, чтобы ``env_vars`` не
    проглотили cwd бэкенда (R7).
    """
    if exec_init:
        init = (
            _BASHRC_EXEC_INIT
            if exec_init == "bashrc"
            else exec_init.rstrip() + "\n"
        )
        body = "bash -c " + shlex.quote(init + command)
    else:
        body = command
    prefix = _env_assignment_prefix(env)
    if prefix:
        body = prefix + body
    if cwd:
        return f"cd {shlex.quote(cwd)} && {body}"
    return body


class HarborBackend(PosixShellFsMixin, FlatWalkMixin, FsBackend):
    is_sandbox = True
    # Файлы живут в контейнере: `exists()` это `test -e` по сети, `read()` —
    # `env.download_file`. Прежний флаг `bridges_to_loop` снят вместе с
    # примесью `AsyncFileOps`, которая его читала: развилки «инлайн или поток»
    # больше нет — операции корутинные и ждут через `_aawait`, то есть на loop
    # владельца и без потока.
    # Дефолт берётся из agent.exec_timeout_sec (harbor bench_defaults → 600).
    default_exec_timeout = None
    #: Фон умеем: команда отцепляется в самом контейнере (см. `aspawn_background`).
    supports_background = True

    def __init__(
        self,
        environment: Any,
        loop: asyncio.AbstractEventLoop,
        *,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
        walk_output_max_chars: int = WALK_OUTPUT_MAX_CHARS,
        exec_init: Optional[str] = None,
        runtime_probe_ttl_sec: int = DEFAULT_RUNTIME_PROBE_TTL_SEC,
    ) -> None:
        self._env = environment
        self._loop = loop
        self.exec_output_capture_max_chars = exec_output_capture_max_chars
        self.walk_output_max_chars = walk_output_max_chars
        self._runtime_probe = RuntimeProbeCache(runtime_probe_ttl_sec)
        # Инициализация окружения для канала МОДЕЛИ (`aexec`): None — сырой
        # shell, "bashrc" — встроенный bootstrap, иначе свой префикс.
        self.exec_init = exec_init
        # WORKDIR контейнера, узнанный `aprime_default_cwd`; до того — None.
        self._discovered_cwd: Optional[str] = None

    async def _aawait(self, coro):
        """То же из асинхронного кода — не занимая ни поток, ни event loop.

        ``self._env`` привязан к loop Harbor, и прямой ``await`` верен только
        если мы на нём же. Мы можем быть на другом (агент со своим
        ``asyncio.run``), поэтому корутина ставится на loop владельца, а
        результат ждётся через ``wrap_future``: у вызывающего это обычный
        ``await``, потока не появляется. Замерено — 5 параллельных вызовов,
        ноль новых потоков, худший разрыв loop 11 мс при шаге пульса 10.
        """
        try:
            running = asyncio.get_running_loop()
        except RuntimeError:
            running = None
        if running is self._loop:
            return await coro
        return await asyncio.wrap_future(
            asyncio.run_coroutine_threadsafe(coro, self._loop)
        )

    def _capture_cap(self, max_chars: Optional[int]) -> int:
        """``None`` → tool-trace cap (40k/поток), else the per-call ceiling."""
        return (
            self.exec_output_capture_max_chars
            if max_chars is None
            else max_chars
        )

    @staticmethod
    def _is_exec_timeout(exc: BaseException) -> bool:
        # Harbor Docker/Apple envs raise RuntimeError("Command timed out after N seconds");
        # also accept bare TimeoutError / asyncio.TimeoutError.
        if isinstance(exc, TimeoutError):
            return True
        return "Command timed out after" in str(exc)

    def _output_capture(
        self, cap: int
    ) -> tuple[ContextManager[None], dict[str, "BoundedStreamAccumulator"]]:
        """Scope Harbor's public ``scoped_output_callback`` around one exec and
        feed the streamed chunks into a bounded head+tail accumulator per stream.

        Returns ``(context_manager, accumulators)``. Without the hook (env does
        not stream) the context is a no-op and the accumulators stay empty —
        empty output on timeout is legal.

        Chunks keep being consumed after the head is full: they are still needed
        for ``total_chars`` and for the ring tail. The accumulator
        runs in the agent's own Python process — nothing is required inside the
        task container.
        """
        accumulators = {
            "stdout": BoundedStreamAccumulator(cap),
            "stderr": BoundedStreamAccumulator(cap),
        }
        scope = getattr(self._env, "scoped_output_callback", None)
        if scope is None:
            return contextlib.nullcontext(), accumulators

        async def on_output(text: str, stream: str) -> None:
            acc = accumulators.get(stream)
            if acc is not None:
                acc.feed(text)

        return scope(on_output), accumulators

    # --- shell ---
    async def _aexec_watching(self, full, *, timeout):
        """Дождаться ``env.exec``. Его ``timeout_sec`` и есть срок жизни команды.

        Стоп хода посреди этого ожидания команду не снимает и строку не
        переписывает. Регистрация без записи в лог нужна, чтобы отмена тула
        не оборвала уже идущий ``env.exec``: жест остаётся здесь, пока команда
        сама не выйдет.
        """

        async def _drive():
            return await self._aawait(
                self._env.exec(full, timeout_sec=timeout)
            )

        stop = current_user_stop()
        if stop is None:
            return await _drive()

        finished = asyncio.Event()

        async def _release() -> None:
            await finished.wait()

        async with stop.arm(
            kind="exec",
            backend=type(self).__name__,
            release=_release,
            log_event=False,
        ):
            try:
                return await _drive()
            finally:
                finished.set()

    @staticmethod
    def _timed_out_result(streamed: dict, cap: int) -> ExecResult:
        """Таймаут = частичное выполнение: то, что команда успела напечатать.

        Идёт тем же потоком вывода, которым Harbor пишет логи прогона: само
        исключение таймаута вывода не несёт.
        """
        return bound_exec_result(
            ExecResult(
                return_code=None,
                timed_out=True,
                stdout_stream=streamed["stdout"].result(),
                stderr_stream=streamed["stderr"].result(),
            ),
            cap,
        )

    @staticmethod
    def _completed_result(res: Any, cap: int) -> ExecResult:
        return bound_exec_result(
            ExecResult(
                stdout=res.stdout or "",
                stderr=res.stderr or "",
                return_code=res.return_code,
                timed_out=bool(getattr(res, "timed_out", False)),
            ),
            cap,
        )

    async def _aprobe(self, command: str, timeout: int) -> ExecResult:
        """Сырой echo пробы: без повторной пробы и без ``cd``."""
        return await self._aexec_impl(command, timeout=timeout, probe=False)

    async def _aexec_impl(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
        exec_init: Optional[str] = None,
        probe: bool = True,
    ) -> ExecResult:
        """Транспорт. ``exec_init`` — только для канала модели (``aexec``).

        Сборка строки — ``build_harbor_shell_command``. Внутренние помощники,
        обход, argv и python зовут транспорт без инициализации: сырой канал.
        Перед действием — проба суммы. ``probe=False`` только у самой пробы.
        """
        if probe:
            await self._runtime_probe.ensure(self._aprobe)
        full = build_harbor_shell_command(
            command, cwd=cwd, env=env, exec_init=exec_init
        )
        cap = self._capture_cap(capture_max_chars)
        scope, streamed = self._output_capture(cap)
        try:
            with scope:
                res = await self._aexec_watching(full, timeout=timeout)
        except Exception as exc:
            if not self._is_exec_timeout(exc):
                raise
            return self._timed_out_result(streamed, cap)
        return self._completed_result(res, cap)

    @staticmethod
    def _python_command(code: str) -> str:
        """Quoted heredoc → тело кода берётся буквально."""
        return f"python3 - <<'GC_HARBOR_PY_EOF'\n{code}\nGC_HARBOR_PY_EOF"

    async def _afs_exec(
        self, command: str, *, timeout: Optional[int] = None
    ) -> ExecResult:
        """Internal FS helper (stat/head/mkdir). Walk ceiling, not the tool-trace cap.

        ЕДИНСТВЕННАЯ форма. Синхронная (`_fs_exec`) снята вместе с `_exec` и
        `_await`: у Harbor файловая операция — это сеть, и ждала она через
        ``run_coroutine_threadsafe(...).result()``. На loop самого Harbor это
        не «блокировка потока», а самоблокировка: поток loop'а ждёт корутину,
        которую выполнить может только он же. Двенадцать ``async def``
        методов (``exists``, ``lexists``, ``is_symlink``, ``realpath``,
        ``getsize``, ``append``, ``copy``, ``makedirs``, ``read_head_bytes``,
        ``walk_entries``, ``listdir`` и через ``sha256``/``stat`` ещё два)
        звали её напрямую.
        """
        return await self._aexec_impl(
            command,
            timeout=timeout,
            capture_max_chars=self.walk_output_max_chars,
        )

    async def _aupload_bytes(self, data: bytes, path: str) -> None:
        # mkdir и upload_file идут мимо ``_aexec_impl``.
        await self._runtime_probe.ensure(self._aprobe)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        try:
            await self._aawait(
                self._env.exec(f"mkdir -p $(dirname {shlex.quote(path)})")
            )
            await self._aawait(self._env.upload_file(tmp_path, path))
        finally:
            tmp_path.unlink(missing_ok=True)

    async def _adownload_bytes(self, path: str) -> bytes:
        await self._runtime_probe.ensure(self._aprobe)
        with tempfile.NamedTemporaryFile("wb", delete=False) as tmp:
            tmp_path = Path(tmp.name)
        try:
            await self._aawait(self._env.download_file(path, tmp_path))
            return tmp_path.read_bytes()
        finally:
            tmp_path.unlink(missing_ok=True)

    # ``is_file`` и ``is_dir`` не перекрываем. Нативные ``env.is_file`` /
    # ``env.is_dir`` сводят любой сбой транспорта к False, и мёртвый контейнер
    # выглядит как отсутствие файла или каталога. Миксин ``_test`` отвечает
    # 0/1 и бросает OSError, если ответа нет.

    async def aexec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """Shell модели: с инициализацией окружения, если она задана."""
        return await self._aexec_impl(
            command,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
            exec_init=self.exec_init,
        )

    async def aexec_raw(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """POSIX без профиля: помощники ФС, grep, пробы, MCP."""
        return await self._aexec_impl(
            command,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    async def aexec_argv(
        self,
        argv: list,
        *,
        stdin: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        # Та же сборка argv → строка шелла, что у синхронного `exec_argv`
        # (общий `argv_to_posix_command`): у песочницы exec идёт через POSIX-шелл.
        return await self._aexec_impl(
            argv_to_posix_command(argv, stdin=stdin),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    async def arun_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        return await self._aexec_impl(
            self._python_command(code),
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )
