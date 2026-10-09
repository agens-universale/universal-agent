"""Physical I/O port for the tool layer: LocalFsBackend (host) and HarborBackend
(task container). Tools keep validation/envelopes; backends only answer "where
does this byte live / where does this command run"."""

import os
import shlex
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Any, AsyncIterator, Callable, Optional, Sequence

from gc_core.backends.capture import CapturedStream, captured_stream_from_text
from gc_core.backends.walk_util import (
    WalkEntry,
    iter_walk_entries_from_flat,
    prune_flat_entries,
)

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

    def __repr__(
        self,
    ) -> str:  # pragma: no cover - только для сообщений об ошибке
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
            raise TypeError(
                "ExecResult: return_code обязателен (None означает timeout)"
            )
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


def _rebound(
    stream: Optional[CapturedStream], text: str, max_chars: int
) -> CapturedStream:
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


def _sync_exec_message(backend: object, name: str) -> str:
    return (
        f"{type(backend).__name__}.{name} синхронной формы не имеет: команду у "
        f"этого бэкенда запускает корутина (a{name}). Ходите через "
        "`gc_core.backends.async_protocol.aexec_port(backend)` — он отдаёт сам "
        "бэкенд, если тот корутинный, и заворачивает чужой синхронный в поток."
    )


class SyncExecNotSupportedError(NotImplementedError):
    """Синхронный запуск команды у бэкенда, который умеет только корутину.

    Отдельный тип, а не голый ``NotImplementedError``: у ``FsBackend`` есть и
    настоящие абстрактные методы, и путать «не реализовано» с «формы нет
    намеренно» дорого — второе означает «позовите a-форму», а не «допишите
    бэкенд».
    """


class CapabilitiesNotWarmedError(RuntimeError):
    """Свойство-проба прочитано до ``await backend.awarm_capabilities()``.

    Отдельный тип, а не голый ``RuntimeError``: вызывающему, который умеет
    работать и без нативного grep, нужно уметь отличить «не прогрето» от
    настоящей поломки бэкенда.
    """


class BackgroundNotSupportedError(NotImplementedError):
    """Фоновый запуск у бэкенда, который так не умеет.

    Отдельный тип по той же причине, что и ``SyncExecNotSupportedError``: это
    не «допишите бэкенд», а «у этой песочницы такой возможности нет, скажите
    об этом модели честно». Раньше метода на порту не было вовсе, и вызов
    через обёртку TUI падал с ``AttributeError`` — то есть сообщением об
    отсутствующем атрибуте вместо ответа тула.
    """


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
    # Exec-семейство идёт через OS-изоляцию (bwrap / sandbox-exec), хотя файлы
    # хостовые: MCP `execute_on: os_backend` обязан идти той же обёрткой.
    isolates_exec: bool = False
    # Backend-preferred default exec timeout; None → tool class default.
    default_exec_timeout: Optional[int] = None
    # Умеет ли бэкенд отцепить команду в фон (см. `aspawn_background`).
    # False по умолчанию: фон есть только там, где мы владеем процессами хоста.
    supports_background: bool = False
    # Потолок RAM-буфера stdout/stderr на поток (spec D2/D8).
    exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS

    async def aspawn_background(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
    ) -> dict:
        """Запустить команду фоном и НЕ ждать её: вернуть как минимум ``pid``.

        Вывод команды не сохраняется: у фонового процесса не должно быть ни
        одной трубы к нам, иначе он погибает вместе с читателем.

        Бэкенд, который проверяет мгновенный отказ, кладёт ещё ``exit_code``:
        ``None`` — команда жива, число — она уже вышла с этим кодом. Нет ключа —
        не проверялось (Harbor).

        Объявлено НА ПОРТУ намеренно, хотя умеют не все бэкенды. Пока метод
        жил только на `LocalFsBackend`, обёртка TUI (`TrackingFsBackend`) его
        не видела, и фоновый режим в TUI падал с `AttributeError` — там, где
        он нужнее всего. Теперь поверхность порта одна, и сторож паритета
        обёртки ловит пропажу сразу.
        """
        raise BackgroundNotSupportedError(
            f"{type(self).__name__} не умеет запускать команды фоном"
        )

    # Кэш ответов проб. Заполняет ЕДИНСТВЕННЫЙ писатель —
    # ``awarm_capabilities``; подклассы могут задать ответы заранее через
    # ``_cap_overrides``, не запуская проб вовсе (FakeBackend).
    #
    # Лока здесь больше нет. Он охранял ленивую пробу из свойства, которой не
    # стало; а ``asyncio.Lock`` на его место не годится — бэкенд переживает
    # свой loop (тесты гоняют один и тот же объект под несколькими
    # ``asyncio.run``), и лок, созданный на умершем loop, отказал бы. Двойной
    # прогрев безвреден: он идемпотентен и стоит лишних проб, не рассогласования.
    _has_find_cache: Optional[bool] = None
    _has_grep_cache: Optional[bool] = None
    _grep_exclude_dir_cache: Optional[bool] = None
    _grep_include_cache: Optional[bool] = None
    _cap_overrides: Optional[dict] = None

    def _cap_override(self, name: str) -> Optional[bool]:
        ov = self._cap_overrides
        if not ov:
            return None
        if name not in ov:
            return None
        return bool(ov[name])

    def _cap(self, name: str, cache_attr: str) -> bool:
        """Ответ пробы: подмена, иначе кэш, иначе ЧЕСТНЫЙ ОТКАЗ.

        Сама проба здесь не запускается. Она корутинная — каждая гоняет
        процессы, а в песочнице это сеть, до ~10 с на пробу, — и запустить её
        из свойства нечем: свойство корутиной не сделаешь.

        Раньше на этом месте стоял ленивый синхронный ``exec`` под
        ``threading.Lock``. Читали свойство ИЗ-ПОД LOOP, то есть четыре
        запуска процесса подряд морозили цикл целиком, а выглядело это как
        зависший агент на первом же инструменте. Прогрев (
        ``awarm_capabilities``) был необязательной оптимизацией — теперь он
        обязательный шаг, и кто его не сделал, получает отказ, а не тихую
        заморозку.
        """
        ov = self._cap_override(name)
        if ov is not None:
            return ov
        cached = getattr(self, cache_attr)
        if cached is not None:
            return bool(cached)
        raise CapabilitiesNotWarmedError(
            f"{type(self).__name__}.{name} прочитано до прогрева проб. "
            "Позовите `await backend.awarm_capabilities()` один раз перед "
            "работой (это делает `fs_find_in_files`), либо задайте "
            "`_cap_overrides` — пробы синхронной формы больше не имеют."
        )

    @property
    def has_find(self) -> bool:
        """Usable ``find -print0 | xargs -0 -r grep`` wire (content search only).

        Gates exactly one thing — the native prune path in
        ``native_grep_refusal``. Listing / by-name / walk go through
        ``backend.walk`` / ``iter_walk`` and never consult this flag.
        """
        return self._cap("has_find", "_has_find_cache")

    @property
    def has_grep(self) -> bool:
        """Usable grep for content search (``-E``, ``-n``, ``-R``)."""
        return self._cap("has_grep", "_has_grep_cache")

    @property
    def grep_exclude_dir(self) -> bool:
        """Internal: ``--exclude-dir=NAME`` hides hits."""
        return self._cap("grep_exclude_dir", "_grep_exclude_dir_cache")

    @property
    def grep_include(self) -> bool:
        """Internal: ``--include='*.ext'`` works."""
        return self._cap("grep_include", "_grep_include_cache")

    async def awarm_capabilities(self) -> None:
        """Прогреть кэш проб. ЕДИНСТВЕННАЯ форма и обязательный шаг.

        Порядок последователен по существу: ``grep_exclude_dir`` и
        ``grep_include`` осмысленны только при работающем grep, и запускать их
        на боксе без него — четыре лишних процесса вместо одного ответа.

        Синхронный близнец снят. Он был обёрткой ``asyncio.to_thread`` вокруг
        чтения четырёх свойств, то есть держал поток пула на всё время проб, а
        существовал ровно потому, что пробы были синхронными.

        Проба, упавшая сама по себе, пишет в кэш ``False`` и прогрев не
        роняет: недоступный бэкенд всплывёт на первом настоящем вызове с
        внятной ошибкой, а не здесь.
        """
        from gc_core.backends.capabilities import (
            aprobe_grep_exclude_dir,
            aprobe_grep_include,
            aprobe_has_find,
            aprobe_has_grep,
        )

        async def _fill(cache_attr: str, probe) -> bool:
            if getattr(self, cache_attr) is not None:
                return bool(getattr(self, cache_attr))
            try:
                value = bool(await probe(self))
            except Exception:
                value = False
            setattr(self, cache_attr, value)
            return value

        await _fill("_has_find_cache", aprobe_has_find)
        has_grep = await _fill("_has_grep_cache", aprobe_has_grep)
        if not has_grep:
            # Зависимые пробы без grep смысла не имеют — закрываем их сразу,
            # иначе чтение свойства после прогрева получит отказ на пустом кэше.
            self._grep_exclude_dir_cache = False
            self._grep_include_cache = False
            return
        await _fill("_grep_exclude_dir_cache", aprobe_grep_exclude_dir)
        await _fill("_grep_include_cache", aprobe_grep_include)

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
    async def stat_info(
        self, path: str, follow_symlinks: bool = False
    ) -> dict: ...
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
    async def write(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None: ...
    @abstractmethod
    async def write_bytes(self, path: str, data: bytes) -> None: ...
    @abstractmethod
    async def append(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None: ...
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
        entries = [
            WalkEntry(rel, is_dir) for rel, is_dir in await self.walk(path)
        ]
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
    #
    # Синхронные `exec` / `exec_argv` / `run_python` — НЕ абстрактные и НЕ
    # часть того, что реализуют наши бэкенды. Они остались точкой входа для
    # ЧУЖИХ реализаций: встраивающий пишет свой синхронный бэкенд (песочница,
    # контейнер, удалённый хост), а ядро видит его через ``SyncExecAdapter``
    # (см. ``backends/async_protocol``). Снять их значило бы сломать каждую
    # такую реализацию — ровно то, чего переезд обязан избежать.
    #
    # Свои бэкенды (local, harbor, обёртка TUI) реализуют ТОЛЬКО корутинную
    # форму: их синхронные двойники были обёртками ``run_coro_blocking``, то
    # есть заводили второй loop, а из-под живого — морозили поток вызывающего
    # на всё время команды (минуты). Умолчание ниже поэтому отказывает громко.
    def exec(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        raise SyncExecNotSupportedError(_sync_exec_message(self, "exec"))

    def exec_raw(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """Механический POSIX: без инициализации окружения образа.

        Вход для ЧУЖИХ синхронных реализаций, как и ``exec``. Канал модели
        может предварять команду профилем (Harbor); внутренние вызовы — пробы,
        grep, обход, MCP — идут этим каналом. У хоста профиля нет, поэтому
        умолчание — тот же ``exec``.
        """
        kwargs = {"cwd": cwd, "timeout": timeout, "env": env}
        if capture_max_chars is not None:
            kwargs["capture_max_chars"] = capture_max_chars
        return self.exec(command, **kwargs)

    def exec_argv(
        self,
        argv: list,
        *,
        stdin: Optional[str] = None,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """Run a program from an argv list, optionally feeding ``stdin``.

        Default builds a POSIX shell string (valid for sandbox backends whose
        exec runs through a POSIX shell). Backends whose exec shell is not
        POSIX (e.g. local Windows cmd.exe) must override with a shell-less
        implementation.

        Goes through ``exec_raw``: argv is a program, not a model shell command.

        ``capture_max_chars=None`` → ``exec_output_capture_max_chars`` (tool
        trace, 40k/поток). Internal post-processed calls (native grep, walk) pass
        the walk ceiling (~50M).
        """
        command = argv_to_posix_command(argv, stdin=stdin)
        kwargs = {"cwd": cwd, "timeout": timeout, "env": env}
        if capture_max_chars is not None:
            kwargs["capture_max_chars"] = capture_max_chars
        # Откат на `exec`, как у develop: чужой бэкенд может не наследовать
        # FsBackend (утиная реализация) и `exec_raw` не знать — у хоста профиля
        # нет, каналы совпадают.
        fn = getattr(self, "exec_raw", None)
        if not callable(fn):
            fn = self.exec
        return fn(command, **kwargs)

    def run_python(
        self,
        code: str,
        *,
        cwd: Optional[str] = None,
        timeout: Optional[int] = None,
        env: Optional[dict] = None,
        capture_max_chars: Optional[int] = None,
    ) -> ExecResult:
        """См. ``exec``: синхронная форма — вход для чужих реализаций."""
        raise SyncExecNotSupportedError(_sync_exec_message(self, "run_python"))

    @abstractmethod
    def default_cwd(self) -> Optional[str]: ...
