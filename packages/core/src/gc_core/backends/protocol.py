"""Physical I/O port for the tool layer: LocalFsBackend (host) and HarborBackend
(task container). Tools keep validation/envelopes; backends only answer "where
does this byte live / where does this command run"."""
import os
import shlex
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, Callable, Iterable, Optional, Sequence

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


class AsyncFileOps:
    """Асинхронные двойники файловых операций — примесь, а не тело порта.

    Примесью, потому что носителей ДВА: сам ``FsBackend`` и обёртки вокруг
    него (``gc_tui.diff_tui.TrackingFsBackend``), которые порт не наследуют,
    но обязаны повторять его поверхность. Копия двадцати пяти методов в
    обёртке разъехалась бы с оригиналом первой же правкой.

    Двойники зовут ``self.<метод>``, а не внутренний бэкенд: у обёртки это её
    собственный, отслеживающий метод.

    Зачем они вообще. Тулы зовут файловые методы ИНЛАЙН внутри ``acall`` — у
    файлов нет нативной асинхронной формы, и поход в пул стоит дороже самой
    работы (40 мкс против микросекунд). Для ``LocalFsBackend`` это так и
    остаётся.

    Но «файл» у бэкенда не обязан быть файлом. У ``HarborBackend``
    ``exists()`` — это ``test -e`` по сети, ``read()`` — ``env.download_file``,
    и оба ЖДУТ корутину ЧУЖОГО loop через
    ``run_coroutine_threadsafe(...).result()``. Позови такой метод инлайн с
    того же loop — поток заблокирует сам себя навсегда. Проверено зависанием,
    когда агента попробовали вести на loop Harbor.

    Отсюда развилка в ОДНОМ месте (``_afile``), а не в двадцати пяти:
    мостящий бэкенд поднимает флаг и получает поток, локальный идёт инлайн.
    Чужой бэкенд не делает ничего — дефолт зовёт его синхронный метод инлайн,
    то есть ровно то, что происходит сегодня.

    ОГРАНИЧЕНИЕ, о котором надо знать до того, как на эти двойники
    рассчитывать: **их пока никто не зовёт**. Все тулы обращаются к
    синхронным методам порта, поэтому под ``HarborBackend`` файловая операция
    в туле — это по-прежнему ``run_coroutine_threadsafe(...).result()`` на
    заблокированном loop агента, то есть сетевой roundtrip на каждую проверку
    пути. Критерий приёмки «разрыв < 150 мс» под Harbor файловыми тулами НЕ
    выполняется, и раньше эта докстрока умалчивала об этом.

    Почему не переведены разом: 93 обращения к порту в тулах, 35 из них — в
    синхронных вспомогательных функциях, которые пришлось бы делать
    корутинными каскадом. Проверить результат нечем — стенда с Harbor нет,
    а менять 93 места вслепую хуже, чем назвать ограничение. Локального
    бэкенда (CLI, TUI, сервер по умолчанию) это не касается вовсе: у него
    ``bridges_to_loop = False``, и инлайн там правильный.
    """

    #: Бэкенд мостит операции на ЧУЖОЙ event loop (Harbor). Синхронный метод у
    #: такого блокирует поток, поэтому двойники обязаны уходить с текущего
    #: потока — иначе дедлок на собственном loop.
    bridges_to_loop: bool = False

    async def _afile(self, fn, *args, **kwargs):
        """Исполнить файловую операцию, не заблокировав СВОЙ loop."""
        if self.bridges_to_loop:
            import asyncio

            return await asyncio.to_thread(lambda: fn(*args, **kwargs))
        return fn(*args, **kwargs)

    async def aexists(self, path):
        return await self._afile(self.exists, path)

    async def alexists(self, path):
        return await self._afile(self.lexists, path)

    async def ais_file(self, path):
        return await self._afile(self.is_file, path)

    async def ais_dir(self, path):
        return await self._afile(self.is_dir, path)

    async def ais_symlink(self, path):
        return await self._afile(self.is_symlink, path)

    async def agetsize(self, path):
        return await self._afile(self.getsize, path)

    async def astat_info(self, path, follow_symlinks=False):
        return await self._afile(self.stat_info, path, follow_symlinks)

    async def areadable(self, path):
        return await self._afile(self.readable, path)

    async def awritable(self, path):
        return await self._afile(self.writable, path)

    async def arealpath(self, path):
        return await self._afile(self.realpath, path)

    async def ais_hidden(self, path):
        return await self._afile(self.is_hidden, path)

    async def aread(self, path):
        return await self._afile(self.read, path)

    async def aread_bytes(self, path):
        return await self._afile(self.read_bytes, path)

    async def aiter_lines(self, path):
        return await self._afile(self.iter_lines, path)

    async def aread_head_bytes(self, path, n):
        return await self._afile(self.read_head_bytes, path, n)

    async def asha256_bytes(self, path):
        return await self._afile(self.sha256_bytes, path)

    async def awrite(self, path, content, encoding='utf-8'):
        return await self._afile(self.write, path, content, encoding)

    async def awrite_bytes(self, path, data):
        return await self._afile(self.write_bytes, path, data)

    async def aappend(self, path, content, encoding='utf-8'):
        return await self._afile(self.append, path, content, encoding)

    async def acopy(self, src, dst):
        return await self._afile(self.copy, src, dst)

    async def amakedirs(self, path):
        return await self._afile(self.makedirs, path)

    async def alistdir(self, path):
        return await self._afile(self.listdir, path)

    async def awalk(self, path):
        return await self._afile(self.walk, path)

    async def _aiter(self, fn, *args, **kwargs):
        """То же, что ``_afile``, но для ГЕНЕРАТОРНЫХ методов.

        Отдельная ветка нужна не для красоты. ``_afile`` уводил в поток вызов
        генераторной функции — а тот возвращается МГНОВЕННО, отдав генератор:
        настоящая работа шла потом, при итерации, на loop вызывающего. Для
        мостящего бэкенда это ровно та блокировка, которую примесь обещает
        снять (``walk`` у Harbor внутри делает ``run_coroutine_threadsafe(...)
        .result()``). Поэтому здесь генератор МАТЕРИАЛИЗУЕТСЯ в потоке, и
        наружу уходит готовый список.

        Цена — память под весь обход; она приемлема ровно там, где эта ветка
        и включается: у мостящего бэкенда каждый шаг обхода стоит сетевого
        обращения, и по сравнению с ним список в памяти ничего не стоит.
        Локальный бэкенд по-прежнему отдаёт ленивый генератор инлайн.
        """
        if self.bridges_to_loop:
            import asyncio

            return await asyncio.to_thread(lambda: list(fn(*args, **kwargs)))
        return fn(*args, **kwargs)

    async def aiter_walk(self, path, *, should_descend=None):
        return await self._aiter(self.iter_walk, path, should_descend=should_descend)

    async def aiter_files(self, path, *, should_descend=None):
        return await self._aiter(self.iter_files, path, should_descend=should_descend)

    async def aiter_walk_entries(self, path, **kwargs):
        return await self._aiter(self.iter_walk_entries, path, **kwargs)

class FsBackend(AsyncFileOps, ABC):
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
    def exists(self, path: str) -> bool: ...
    @abstractmethod
    def lexists(self, path: str) -> bool: ...
    @abstractmethod
    def is_file(self, path: str) -> bool: ...
    @abstractmethod
    def is_dir(self, path: str) -> bool: ...
    @abstractmethod
    def is_symlink(self, path: str) -> bool: ...
    @abstractmethod
    def getsize(self, path: str) -> int: ...
    @abstractmethod
    def stat_info(self, path: str, follow_symlinks: bool = False) -> dict: ...
    @abstractmethod
    def readable(self, path: str) -> bool: ...
    @abstractmethod
    def writable(self, path: str) -> bool: ...
    @abstractmethod
    def realpath(self, path: str) -> str: ...
    @abstractmethod
    def is_hidden(self, path: str) -> bool: ...

    # --- read ---
    @abstractmethod
    def read(self, path: str) -> str: ...
    @abstractmethod
    def read_bytes(self, path: str) -> bytes: ...
    @abstractmethod
    def iter_lines(self, path: str) -> Iterable[str]: ...
    @abstractmethod
    def read_head_bytes(self, path: str, n: int) -> bytes: ...
    @abstractmethod
    def sha256_bytes(self, path: str) -> str: ...

    # --- write ---
    @abstractmethod
    def write(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    def write_bytes(self, path: str, data: bytes) -> None: ...
    @abstractmethod
    def append(self, path: str, content: str, encoding: str = "utf-8") -> None: ...
    @abstractmethod
    def copy(self, src: str, dst: str) -> None: ...
    @abstractmethod
    def makedirs(self, path: str) -> None: ...

    # --- traverse ---
    @abstractmethod
    def listdir(self, path: str) -> list: ...
    @abstractmethod
    def walk(self, path: str) -> list: ...

    @abstractmethod
    def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]: ...

    @abstractmethod
    def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]: ...

    def walk_entries(
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
        entries = [WalkEntry(rel, is_dir) for rel, is_dir in self.walk(path)]
        return prune_flat_entries(
            entries, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
        )

    def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> Iterable[WalkEntry]:
        """``iter_walk`` order over ``walk_entries`` (sizes preserved)."""
        yield from iter_walk_entries_from_flat(
            self.walk_entries(path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs),
            should_descend=should_descend,
            join=self.join,
            root=path,
        )

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
