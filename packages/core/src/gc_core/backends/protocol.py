"""Physical I/O port for the tool layer: LocalFsBackend (host) and HarborBackend
(task container). Tools keep validation/envelopes; backends only answer "where
does this byte live / where does this command run"."""
import os
import shlex
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Iterable, Optional, Sequence

from gc_core.backends.capture import CapturedStream, captured_stream_from_text
from gc_core.backends.walk_util import WalkEntry, iter_walk_entries_from_flat, prune_flat_entries

# Потолок captured-буфера на поток (decoded UTF-8 chars): L = head H + tail T,
# H = floor(L / 2), T = L - H. По умолчанию 40k на поток = 20k + 20k.
DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS = 40_000

# Размер каждого края front-loaded preview. Обязано выполняться P <= T.
DEFAULT_EXEC_OUTPUT_PREVIEW_EDGE_CHARS = 3_000

# Harbor walk/listdir private channel (B1). Not tool exec; fail-closed on hit.
WALK_OUTPUT_MAX_CHARS = 50_000_000

# Wire format of the harbor walk/listdir listing (find -printf): relpath, type, size.
# Shell form for ``-printf '…'`` — find itself interprets the escapes.
FIND_PRINTF_FORMAT = "%P\\t%y\\t%s\\n"


class WalkOutputTooLargeError(OSError):
    """Harbor traverse stdout hit walk ceiling — tree is incomplete (fail-closed)."""

    def __init__(
        self,
        path: str,
        *,
        ceiling_chars: int = WALK_OUTPUT_MAX_CHARS,
        message: Optional[str] = None,
    ) -> None:
        self.path = path
        self.ceiling_chars = ceiling_chars
        msg = message or (
            f"directory listing under {path!r} is incomplete: it exceeded the "
            f"walk output ceiling ({ceiling_chars} chars). This is not proof "
            f"that a path or tree is absent — narrow the path, reduce depth, "
            f"do not search from the repository root"
        )
        super().__init__(msg)


class _Missing:
    """Отличает «не передали return_code» от осознанного ``None`` (timeout)."""

    def __repr__(self) -> str:  # pragma: no cover - только для сообщений об ошибке
        return "<missing>"


_MISSING = _Missing()


@dataclass
class ExecResult:
    """Результат exec: плоский текст для внутренних потребителей + структура потока.

    ``stdout`` / ``stderr`` — наблюдённый текст (при обрезке это ``head + tail``
    без синтетического маркера). Настоящие края, точные счётчики и признак
    отброшенной середины живут в ``stdout_stream`` / ``stderr_stream``; их и
    читает слой exec-тулы. Prefix-only семантики больше нет: хвост настоящий.
    """

    stdout: str = ""
    stderr: str = ""
    return_code: Any = _MISSING
    timed_out: bool = False
    # Сохранён для потребителей вне exec (fs-поиск, MCP-обёртка): им
    # нужен только факт «поток обрезан», а не края. Выводится из потока.
    stdout_hit_capture_cap: bool = False
    stderr_hit_capture_cap: bool = False
    stdout_stream: Optional[CapturedStream] = None
    stderr_stream: Optional[CapturedStream] = None

    def __post_init__(self) -> None:
        if isinstance(self.return_code, _Missing):
            # Молчаливый None превратил бы успешную команду в non_zero_exit
            # "exited with status None" — падаем на конструировании, как раньше.
            raise TypeError("ExecResult: return_code обязателен (None означает timeout)")
        if self.stdout_stream is None:
            self.stdout_stream = CapturedStream(
                head=self.stdout or "", total_chars=len(self.stdout or "")
            )
        else:
            self.stdout = self.stdout_stream.text
        if self.stderr_stream is None:
            self.stderr_stream = CapturedStream(
                head=self.stderr or "", total_chars=len(self.stderr or "")
            )
        else:
            self.stderr = self.stderr_stream.text
        self.stdout_hit_capture_cap = (
            bool(self.stdout_hit_capture_cap) or self.stdout_stream.truncated
        )
        self.stderr_hit_capture_cap = (
            bool(self.stderr_hit_capture_cap) or self.stderr_stream.truncated
        )


def bound_captured_stream(text: str, max_chars: int) -> tuple[str, bool]:
    """Нормализовать готовый поток head+tail; вернуть ``(text, truncated)``."""
    cs = captured_stream_from_text(text, max_chars)
    return cs.text, cs.truncated


def _rebound(stream: Optional[CapturedStream], text: str, max_chars: int) -> CapturedStream:
    # Поток из accumulator с отброшенной серединой пересобирать нельзя: его
    # плоский текст — уже склейка краёв, total_chars восстановить неоткуда.
    if stream is not None and stream.truncated:
        return stream
    return captured_stream_from_text(text or "", max_chars)


def bound_exec_result(result: ExecResult, max_chars: int) -> ExecResult:
    """Один контракт local/harbor: готовый результат — тем же head+tail алгоритмом.

    Внешняя форма совпадает, но bounded RAM end-to-end это не даёт, если чужая
    реализация уже накопила полный вывод до возврата.
    """
    stdout_stream = _rebound(result.stdout_stream, result.stdout, max_chars)
    stderr_stream = _rebound(result.stderr_stream, result.stderr, max_chars)
    return ExecResult(
        return_code=result.return_code,
        timed_out=result.timed_out,
        stdout_stream=stdout_stream,
        stderr_stream=stderr_stream,
    )



def argv_to_posix_command(argv: list, *, stdin: Optional[str] = None) -> str:
    """``argv`` (плюс необязательный ``stdin``) → строка для POSIX-шелла.

    Отдельной функцией, а не телом ``exec_argv``: ту же сборку обязаны
    использовать асинхронные реализации бэкендов. Копия неизбежно разъехалась
    бы с оригиналом — а расхождение здесь означало бы, что синхронный и
    асинхронный вызовы запускают РАЗНЫЕ команды.
    """
    command = " ".join(shlex.quote(str(a)) for a in argv)
    if stdin is not None:
        command = f"printf '%s' {shlex.quote(stdin)} | {command}"
    return command

class FsBackend(ABC):
    """Физический порт ввода-вывода. Файловые операции — КОРУТИНЫ.

    Форма одна. Раньше их было две: синхронные методы порта и примесь
    ``AsyncFileOps`` с двадцатью восемью двойниками поверх них. Двойников не
    звал никто, кроме их собственного теста, — а тулы шли синхронным путём.

    Для ``LocalFsBackend`` это было безобидно: у файла нет нативной
    асинхронной формы, и работа идёт инлайн, за микросекунды. Но «файл» у
    бэкенда не обязан быть файлом. У ``HarborBackend`` ``exists()`` — это
    ``test -e`` по сети, а ``read()`` — скачивание, и синхронная форма ждала
    их через ``run_coroutine_threadsafe(...).result()``, то есть блокировала
    поток. Позови такой метод инлайн с loop самого Harbor — поток заблокирует
    сам себя навсегда (проверено зависанием). Именно поэтому критерий
    «разрыв < 150 мс» под Harbor файловыми тулами не выполнялся: каждая
    проверка пути была сетевым roundtrip'ом на замороженном loop.

    Теперь у Harbor те же методы ждут нативно (``_aawait``: корутина уходит на
    loop владельца, результат — обычным ``await``, без потока), а у локального
    бэкенда тело осталось прежним, инлайн. Расплата — ``await`` на вызове
    даже там, где работа занимает микросекунды; она дешевле двух форм.

    Обходчики (``iter_lines``, ``iter_walk``, ``iter_files``,
    ``iter_walk_entries``) — асинхронные генераторы: ``async for``.
    """

    # Self-isolated sandbox (task container); tools use it to skip host-only
    # affordances. Security guards do NOT read this flag.
    is_sandbox: bool = False
    # Backend-preferred default exec timeout; None → tool class default.
    default_exec_timeout: Optional[int] = None
    # Потолок RAM-буфера stdout/stderr на поток (spec D2/D8).
    exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS

    # Capability probe cache (lazy, thread-safe once). Subclasses may set
    # overrides via ``_cap_overrides`` without running probes (FakeBackend).
    _cap_lock: Optional[threading.Lock] = None
    _has_find_cache: Optional[bool] = None
    _has_grep_cache: Optional[bool] = None
    _grep_exclude_dir_cache: Optional[bool] = None
    _grep_include_cache: Optional[bool] = None
    _cap_overrides: Optional[dict] = None

    def _ensure_cap_lock(self) -> threading.Lock:
        if self._cap_lock is None:
            self._cap_lock = threading.Lock()
        return self._cap_lock

    def _cap_override(self, name: str) -> Optional[bool]:
        ov = self._cap_overrides
        if not ov:
            return None
        if name not in ov:
            return None
        return bool(ov[name])

    @property
    def has_find(self) -> bool:
        """Usable ``find -print0 | xargs -0 -r grep`` wire (content search only).

        Gates exactly one thing — the native prune path in
        ``native_grep_refusal``. Listing / by-name / walk go through
        ``backend.walk`` / ``iter_walk`` and never consult this flag.
        """
        ov = self._cap_override("has_find")
        if ov is not None:
            return ov
        if self._has_find_cache is not None:
            return self._has_find_cache
        with self._ensure_cap_lock():
            if self._has_find_cache is not None:
                return self._has_find_cache
            try:
                from gc_core.backends.capabilities import probe_has_find

                self._has_find_cache = bool(probe_has_find(self))
            except Exception:
                self._has_find_cache = False
            return self._has_find_cache

    @property
    def has_grep(self) -> bool:
        """Usable grep for content search (``-E``, ``-n``, ``-R``)."""
        ov = self._cap_override("has_grep")
        if ov is not None:
            return ov
        if self._has_grep_cache is not None:
            return self._has_grep_cache
        with self._ensure_cap_lock():
            if self._has_grep_cache is not None:
                return self._has_grep_cache
            try:
                from gc_core.backends.capabilities import probe_has_grep

                self._has_grep_cache = bool(probe_has_grep(self))
            except Exception:
                self._has_grep_cache = False
            return self._has_grep_cache

    @property
    def grep_exclude_dir(self) -> bool:
        """Internal: ``--exclude-dir=NAME`` hides hits (lazy smoke)."""
        ov = self._cap_override("grep_exclude_dir")
        if ov is not None:
            return ov
        if self._grep_exclude_dir_cache is not None:
            return self._grep_exclude_dir_cache
        # Read the dependency BEFORE taking the lock: has_grep takes the same
        # non-reentrant lock, so reading it inside would deadlock on a cold cache.
        if not self.has_grep:
            self._grep_exclude_dir_cache = False
            return False
        with self._ensure_cap_lock():
            if self._grep_exclude_dir_cache is not None:
                return self._grep_exclude_dir_cache
            try:
                from gc_core.backends.capabilities import probe_grep_exclude_dir

                self._grep_exclude_dir_cache = bool(probe_grep_exclude_dir(self))
            except Exception:
                self._grep_exclude_dir_cache = False
            return self._grep_exclude_dir_cache

    @property
    def grep_include(self) -> bool:
        """Internal: ``--include='*.ext'`` works (lazy smoke)."""
        ov = self._cap_override("grep_include")
        if ov is not None:
            return ov
        if self._grep_include_cache is not None:
            return self._grep_include_cache
        # Same reason as grep_exclude_dir: has_grep must not be read under the lock.
        if not self.has_grep:
            self._grep_include_cache = False
            return False
        with self._ensure_cap_lock():
            if self._grep_include_cache is not None:
                return self._grep_include_cache
            try:
                from gc_core.backends.capabilities import probe_grep_include

                self._grep_include_cache = bool(probe_grep_include(self))
            except Exception:
                self._grep_include_cache = False
            return self._grep_include_cache

    def warm_capabilities(self) -> None:
        """Прогреть кэш проб: четыре свойства читаются подряд, здесь и сейчас.

        Свойства остаются СИНХРОННЫМИ намеренно. Сделать их асинхронными
        значит превратить часть протокола ``FsBackend`` в async-аксессоры —
        правку контракта, которую видит каждый встраивающий, ради разового
        выигрыша: пробы кэшируются, то есть блокируют однократно на процесс.

        Вместо этого прогрев уводится с ГОРЯЧЕГО пути. Без него первая же
        `fs_find_in_files` платит за четыре пробы (до ~10 с на пробу в
        медленной песочнице) прямо внутри поиска, и агент выглядит зависшим на
        первом инструменте.
        """
        for name in ("has_find", "has_grep", "grep_exclude_dir", "grep_include"):
            try:
                getattr(self, name)
            except Exception:
                # Проба сама по себе не должна ронять прогрев: неудачная
                # запишет False в кэш, а недоступный бэкенд всплывёт на первом
                # настоящем вызове с внятной ошибкой, а не здесь.
                pass

    async def awarm_capabilities(self) -> None:
        """Прогрев, не занимая event loop вызывающего.

        Все четыре пробы уходят в ОДИН поток, а не в четыре: они и так
        последовательны (``grep_include`` ждёт ``has_grep``), и делить их
        между слотами общего пула незачем.
        """
        import asyncio

        await asyncio.to_thread(self.warm_capabilities)

    # --- stat ---
    @abstractmethod
    async def exists(self, path: str) -> bool: ...
    @abstractmethod
    async def lexists(self, path: str) -> bool: ...
    @abstractmethod
    async def is_file(self, path: str) -> bool: ...
    @abstractmethod
    async def is_dir(self, path: str) -> bool: ...
    @abstractmethod
    async def is_symlink(self, path: str) -> bool: ...
    @abstractmethod
    async def getsize(self, path: str) -> int: ...
    @abstractmethod
    async def stat_info(self, path: str, follow_symlinks: bool = False) -> dict: ...
    @abstractmethod
    async def readable(self, path: str) -> bool: ...
    @abstractmethod
    async def writable(self, path: str) -> bool: ...
    @abstractmethod
    async def realpath(self, path: str) -> str: ...
    @abstractmethod
    async def is_hidden(self, path: str) -> bool: ...

    # --- read ---
    @abstractmethod
    async def read(self, path: str) -> str: ...
    @abstractmethod
    async def read_bytes(self, path: str) -> bytes: ...
    @abstractmethod
    # Обходчики объявлены обычным `def`: асинхронный генератор — НЕ корутина,
    # его вызов не ждут, по нему идут `async for`. Реализации пишут
    # `async def` с `yield`; сигнатура порта обещает ровно это.
    def iter_lines(self, path: str) -> AsyncIterator[str]: ...
    @abstractmethod
    async def read_head_bytes(self, path: str, n: int) -> bytes: ...
    @abstractmethod
    async def sha256_bytes(self, path: str) -> str: ...

    # --- write ---
    @abstractmethod
    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    @abstractmethod
    async def append(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    async def copy(self, src: str, dst: str) -> None: ...
    @abstractmethod
    async def makedirs(self, path: str) -> None: ...

    # --- traverse ---
    @abstractmethod
    async def listdir(self, path: str) -> list: ...
    @abstractmethod
    async def walk(self, path: str) -> list: ...

    @abstractmethod
    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> AsyncIterator[tuple[str, bool]]: ...

    @abstractmethod
    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> AsyncIterator[str]: ...

    async def walk_entries(
        self,
        path: str,
        *,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> list:
        """Flat recursive listing as ``WalkEntry`` rows, sizes when the backend
        learns them during the traversal.

        Default: from ``walk()`` — no sizes (tools fall back to ``getsize``) and
        host-side prune, so every backend yields the same tree for the same rules.
        """
        entries = [WalkEntry(rel, is_dir) for rel, is_dir in await self.walk(path)]
        return prune_flat_entries(
            entries, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
        )

    async def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> AsyncIterator[WalkEntry]:
        """``iter_walk`` order over ``walk_entries`` (sizes preserved)."""
        for entry in iter_walk_entries_from_flat(
            await self.walk_entries(
                path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
            ),
            should_descend=should_descend,
            join=self.join,
            root=path,
        ):
            yield entry

    # --- path semantics ---
    @abstractmethod
    def normpath(self, path: str) -> str: ...
    @abstractmethod
    def join(self, base: str, name: str) -> str: ...

    def isabs(self, path: str) -> bool:
        """Is ``path`` absolute in THIS backend's path syntax.

        Tools validate the argument before touching the backend, so the answer
        must come from the backend, not from the host: a sandbox speaks POSIX
        regardless of where the agent process runs (and CPython 3.13 stopped
        calling ``/x`` absolute on Windows). Host default; override where the
        syntax differs.
        """
        return os.path.isabs(path)

    # --- exec ---
    @abstractmethod
    def exec(self, command: str, *, cwd: Optional[str] = None,
             timeout: Optional[int] = None,
             env: Optional[dict] = None,
             capture_max_chars: Optional[int] = None) -> ExecResult: ...

    def exec_argv(self, argv: list, *, stdin: Optional[str] = None,
                  cwd: Optional[str] = None,
                  timeout: Optional[int] = None,
                  env: Optional[dict] = None,
                  capture_max_chars: Optional[int] = None) -> ExecResult:
        """Run a program from an argv list, optionally feeding ``stdin``.

        Default builds a POSIX shell string (valid for sandbox backends whose
        exec runs through a POSIX shell). Backends whose exec shell is not
        POSIX (e.g. local Windows cmd.exe) must override with a shell-less
        implementation.

        ``capture_max_chars=None`` → ``exec_output_capture_max_chars`` (tool
        trace, 40k/поток). Internal post-processed calls (native grep, walk) pass
        the walk ceiling (~50M).
        """
        command = argv_to_posix_command(argv, stdin=stdin)
        if capture_max_chars is None:
            return self.exec(command, cwd=cwd, timeout=timeout, env=env)
        return self.exec(
            command,
            cwd=cwd,
            timeout=timeout,
            env=env,
            capture_max_chars=capture_max_chars,
        )

    @abstractmethod
    def run_python(self, code: str, *, cwd: Optional[str] = None,
                   timeout: Optional[int] = None,
                   env: Optional[dict] = None,
                   capture_max_chars: Optional[int] = None) -> ExecResult: ...
    @abstractmethod
    def default_cwd(self) -> Optional[str]: ...
