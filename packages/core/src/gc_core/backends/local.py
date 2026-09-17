"""Host implementation of FsBackend (os / open() / subprocess); the host-I/O
details that used to live inline in the tools are concentrated here."""
import asyncio
import signal
import codecs
import hashlib
import os
import stat as stat_module
import time
import sys
from typing import Any, Callable, Dict, Iterable, Optional, Sequence

from gc_core.backends.capture import BoundedStreamAccumulator, CapturedStream
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ExecResult,
    FsBackend,
)
from gc_core.backends.walk_util import WalkEntry, pruned_dir_predicate
from gc_core.utils.file_text import encode_text_for_append, encode_text_for_write, read_text_for_edit


def _is_real_dir(entry: os.DirEntry) -> bool:
    try:
        return entry.is_dir(follow_symlinks=False)
    except OSError:
        return False


def _size_without_following(entry: os.DirEntry) -> Optional[int]:
    try:
        if entry.is_symlink():
            return None
        return entry.stat(follow_symlinks=False).st_size
    except OSError:
        return None


class LocalFsBackend(FsBackend):
    is_sandbox = False
    default_exec_timeout = None
    #: Умеем запускать фоном: процесс отцепляется в свою сессию, вывод идёт в
    #: файл. У порта по умолчанию False — бэкенд песочницы (Harbor) так не
    #: умеет, и тул обязан сказать об этом честно, а не падать AttributeError.
    supports_background = True

    def __init__(
        self,
        *,
        exec_output_capture_max_chars: int = DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    ) -> None:
        self.exec_output_capture_max_chars = exec_output_capture_max_chars

    # --- stat ---
    async def exists(self, path: str) -> bool:
        return os.path.exists(path)

    async def lexists(self, path: str) -> bool:
        return os.path.lexists(path)

    async def is_file(self, path: str) -> bool:
        return os.path.isfile(path)

    async def is_dir(self, path: str) -> bool:
        return os.path.isdir(path)

    async def is_symlink(self, path: str) -> bool:
        return os.path.islink(path)

    async def getsize(self, path: str) -> int:
        return os.path.getsize(path)

    async def stat_info(self, path: str, follow_symlinks: bool = False) -> dict:
        st = os.stat(path, follow_symlinks=follow_symlinks)
        return {
            "size": st.st_size,
            "mtime": st.st_mtime,
            "mode": st.st_mode,
            "is_file": stat_module.S_ISREG(st.st_mode),
            "is_dir": stat_module.S_ISDIR(st.st_mode),
            "is_symlink": os.path.islink(path),
        }

    async def readable(self, path: str) -> bool:
        return os.access(path, os.R_OK)

    async def writable(self, path: str) -> bool:
        return os.access(path, os.W_OK)

    async def realpath(self, path: str) -> str:
        return os.path.realpath(path)

    async def is_hidden(self, path: str) -> bool:
        """Точка в имени ИЛИ скрытый атрибут Windows.

        Поле информационное (метка в выдаче list/by-name), и его значение обязано
        совпадать с песочницей — там скрытость определяется только точкой
        (тест паритета list_files это и требует). Одной атрибутной проверки мало:
        на Windows у `.env` / `.gitignore` атрибута нет, а служебными они остаются.
        """
        basename = os.path.basename(path)
        if basename.startswith("."):
            return True
        if os.name == "nt":
            try:
                attrs = os.stat(path).st_file_attributes  # Windows-specific
            except (AttributeError, OSError):
                return False
            return bool(attrs & stat_module.FILE_ATTRIBUTE_HIDDEN)
        return False

    # --- read ---
    async def read(self, path: str) -> str:
        return read_text_for_edit(path)

    async def read_bytes(self, path: str) -> bytes:
        with open(path, "rb") as f:
            return f.read()

    async def iter_lines(self, path: str) -> Iterable[str]:
        # Preserve raw line endings; same codec as read_text_for_edit.
        text = await self.read(path)
        for _item in text.splitlines(keepends=True):
            yield _item

    async def read_head_bytes(self, path: str, n: int) -> bytes:
        with open(path, "rb") as f:
            return f.read(n)

    async def sha256_bytes(self, path: str) -> str:
        digest = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                digest.update(block)
        return digest.hexdigest()

    # --- write ---
    async def write(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Allowlist + strict encode → atomic bytes write (no errors=replace).
        await self.write_bytes(path, encode_text_for_write(content, encoding))

    async def write_bytes(self, path: str, data: bytes) -> None:
        tmp_path = f"{path}.tmp"
        try:
            with open(tmp_path, "wb") as fd:
                fd.write(data)
            os.replace(tmp_path, path)
        except Exception:
            try:
                if os.path.exists(tmp_path):
                    os.remove(tmp_path)
            except Exception:
                pass
            raise

    async def append(self, path: str, content: str, encoding: str = "utf-8") -> None:
        # Append payload WITHOUT a leading BOM: a second BOM in the middle of an
        # existing utf-16 file decodes as U+FEFF (ZERO WIDTH NO-BREAK SPACE).
        with open(path, "ab") as fd:
            fd.write(encode_text_for_append(content, encoding))

    async def copy(self, src: str, dst: str) -> None:
        with open(src, "rb") as s, open(dst, "wb") as d:
            d.write(s.read())

    async def makedirs(self, path: str) -> None:
        os.makedirs(path, exist_ok=True)

    # --- traverse ---
    async def listdir(self, path: str) -> list:
        entries = []
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            try:
                st = os.lstat(full)  # lstat: symlink-to-dir counts as file (base parity)
                is_dir = stat_module.S_ISDIR(st.st_mode)
            except OSError:
                is_dir = False
            entries.append((name, is_dir))
        return entries

    async def iter_walk(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[tuple[str, bool]]:
        """``iter_walk_entries`` without sizes (dirs first, then files per level)."""
        async for entry in self.iter_walk_entries(path, should_descend=should_descend):
            yield entry.rel, entry.is_dir

    async def iter_files(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
    ) -> Iterable[str]:
        """Stream file relpaths in native ``os.walk`` filename order."""
        for dirpath, dirnames, filenames in os.walk(path, followlinks=False):
            dirnames.sort()
            filenames.sort()
            rel_dir = os.path.relpath(dirpath, path)
            prefix = "" if rel_dir == "." else rel_dir.replace(os.sep, "/") + "/"
            for f in filenames:
                yield prefix + f

            if should_descend is not None:
                dirnames[:] = [
                    d for d in dirnames
                    if should_descend(os.path.join(dirpath, d))
                ]

    async def walk(self, path: str) -> list:
        return [_item async for _item in self.iter_walk(path)]

    async def iter_walk_entries(
        self,
        path: str,
        *,
        should_descend: Optional[Callable[[str], bool]] = None,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> Iterable[WalkEntry]:
        """Top-down DFS over ``os.scandir``: dirs first, then files per level (sorted).

        Sizes come from the directory listing (``DirEntry.stat(follow_symlinks=False)``:
        free on Windows, one ``lstat`` elsewhere) — no second stat per file. A
        symlink's size is unknown (``None``), never the link's own length. A pruned
        or non-descendable directory is still listed; only its subtree is skipped.
        Symlinks are never followed (``os.walk(followlinks=False)`` parity).
        """
        pruned = pruned_dir_predicate(prune_names, prune_dot_dirs)

        def _level(abs_dir: str, prefix: str) -> Iterable[WalkEntry]:
            try:
                with os.scandir(abs_dir) as it:
                    children = sorted(it, key=lambda e: e.name)
            except OSError:
                return
            dirs = [e for e in children if _is_real_dir(e)]
            files = [e for e in children if not _is_real_dir(e)]
            for d in dirs:
                yield WalkEntry(prefix + d.name, True)
            for f in files:
                yield WalkEntry(prefix + f.name, False, _size_without_following(f))
            for d in dirs:
                if pruned(d.name):
                    continue
                full = os.path.join(abs_dir, d.name)
                if should_descend is not None and not should_descend(full):
                    continue
                for _item in _level(full, prefix + d.name + "/"):
                    yield _item

        for _item in _level(path, ""):
            yield _item

    async def walk_entries(
        self,
        path: str,
        *,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> list:
        return [_item async for _item in self.iter_walk_entries(path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs)]

    # --- path semantics ---
    def normpath(self, path: str) -> str:
        return os.path.normpath(path)

    def join(self, base: str, name: str) -> str:
        return os.path.join(base, name)

    def _exec_capture_max_chars(self, capture_max_chars: Optional[int]) -> int:
        if capture_max_chars is None:
            return self.exec_output_capture_max_chars
        return capture_max_chars

    # --- exec ---
    # --- exec (async) ---
    async def aexec(self, command: str, *, cwd: Optional[str] = None,
                    timeout: Optional[int] = None,
                    env: Optional[dict] = None,
                    capture_max_chars: Optional[int] = None) -> ExecResult:
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return await _arun_bounded_process(
            command, shell=True, cwd=cwd, timeout=timeout, env=merged_env,
            stdin_text=None,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    async def aexec_argv(self, argv: list, *, stdin: Optional[str] = None,
                         cwd: Optional[str] = None,
                         timeout: Optional[int] = None,
                         env: Optional[dict] = None,
                         capture_max_chars: Optional[int] = None) -> ExecResult:
        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        return await _arun_bounded_process(
            [str(a) for a in argv], shell=False, cwd=cwd, timeout=timeout,
            env=merged_env, stdin_text=stdin,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    async def arun_python(self, code: str, *, cwd: Optional[str] = None,
                          timeout: Optional[int] = None,
                          env: Optional[dict] = None,
                          capture_max_chars: Optional[int] = None) -> ExecResult:
        return await _arun_bounded_process(
            [sys.executable, "-u", "-"], shell=False, cwd=cwd, timeout=timeout,
            env=_sanitize_env(env), stdin_text=code,
            capture_max_chars=self._exec_capture_max_chars(capture_max_chars),
        )

    async def aspawn_background(self, command: str, *, cwd: Optional[str] = None,
                                env: Optional[dict] = None,
                                log_fd: Optional[int] = None) -> dict:
        """Запустить команду и НЕ ждать её: отдать pid сразу.

        Здесь нет ни труб, ни чтения, ни таймаута, и в этом весь смысл. Труба
        требует живого читателя: как только он исчезает — кончился ход, вышел
        процесс агента, — потомок получает EPIPE или встаёт на заполненном
        буфере. Долгоживущему процессу читатель не нужен вовсе, поэтому его
        вывод идёт в файл, открытый ВЫЗЫВАЮЩИМ (решение принимает гард записи,
        а он живёт на слое тулы), либо не идёт никуда.

        Своя сессия обязательна: без неё потомок остался бы в нашей группе
        процессов и получал бы адресованные нам сигналы.
        """
        import subprocess

        from gc_core.utils.win_console import preserve_console_mode

        merged_env = {**os.environ, **{str(k): str(v) for k, v in env.items()}} if env else None
        sink = log_fd if log_fd is not None else subprocess.DEVNULL
        # stderr — туда же, куда stdout: у фоновой команды разделять потоки
        # некому, а один лог читается человеком и моделью как одна лента.
        err_sink = subprocess.STDOUT if log_fd is not None else subprocess.DEVNULL
        spawn_kwargs = {"start_new_session": True} if os.name != "nt" else {}
        _reap_finished_background()
        with preserve_console_mode():
            # `Popen`, а НЕ `asyncio.create_subprocess_shell`. Асинхронный
            # подпроцесс привязан к наблюдателю за потомками и к транспорту
            # своего loop — то есть к loop ХОДА. На Linux финализатор
            # транспорта при выходе интерпретатора шлёт процессу kill, и
            # фоновая команда погибала вместе с агентом; на macOS она выживала,
            # но в stderr сыпалось «Loop … is closed». Фоновому процессу loop
            # не нужен вовсе: мы его не ждём и не читаем.
            proc = subprocess.Popen(                       # noqa: S602 — shell намеренно
                command,
                shell=True,
                stdout=sink, stderr=err_sink,
                stdin=subprocess.DEVNULL,
                cwd=cwd, env=merged_env,
                **spawn_kwargs,
            )
        _BACKGROUND_PROCS.add(proc)
        # Уступка loop. `Popen` её не делает сам, а на этом пути нет больше ни
        # одного `await`: пачка фоновых запусков шла бы одним неразрывным куском.
        # Замерено на 20 запусках: 26–32 мс худшего разрыва против 3 мс у
        # прежнего asyncio-пути, который отдавал управление внутри себя. Одна
        # уступка возвращает паритет (5–7 мс), а стоит она ничего.
        await asyncio.sleep(0)
        return {"pid": proc.pid}

    def default_cwd(self) -> Optional[str]:
        return os.getcwd()


# --- bounded process runner (spec D1/D2/D6/D9) ---
#
# Вывод команды ПЕРЕДНЕГО ПЛАНА идёт в трубы и читается корутинами. Это не
# возврат к прежнему, а разделение двух разных случаев: команда переднего плана
# живёт секунды-минуты, её вывод нужен сейчас, и переживать нас она не должна.
# Долгоживущая запускается отдельным фоновым режимом, где вывод уходит в
# именованный лог, а труб нет вовсе.
#
# У трубы есть то, чего нет у файла: обратное давление. Читать её ОБЯЗАТЕЛЬНО —
# полный буфер останавливает запись в потомке, — и каждое чтение это `await`,
# то есть уступка loop неустранима по построению. Файловый захват, стоявший
# здесь между этими двумя редакциями, обратного давления не имел: цикл чтения
# не отдавал управление, пока команда писала, и вместе с loop замирали дедлайн
# этой же команды, соседние агенты и keep-alive MCP-сессий.

_READ_CHUNK = 8192
#: Сколько ждать дочитывания ПОСЛЕ выхода потомка. EOF в трубе наступает, когда
#: её закрыл последний писатель, а фоновый внук держит унаследованный конец
#: сколько угодно долго: ждать его бесконечно значит держать tool-call.
_DRAIN_JOIN_TIMEOUT_SEC = 2

#: Сколько даём команде на самостоятельный выход по SIGTERM, прежде чем послать
#: SIGKILL. SIGKILL не перехватывается, и без этой паузы корректная команда не
#: успевает снять блокировку и дописать файл — остаются `.git/index.lock`,
#: недокачанные слои, полузаписанные архивы. Повреждённое состояние обходится
#: дороже, чем полсекунды ожидания.
_KILL_GRACE_SEC = 0.5

#: Сколько ждать выхода уже убитого потомка, прежде чем отпускать дренаж.
#: Сигнал послан, речь о миллисекундах; потолок здесь — от зависшего в
#: непрерываемом системном вызове процесса, а не от нормального случая.
_KILL_EXIT_GRACE_SEC = 1.0

#: Запущенные фоном процессы. Держим ссылку, чтобы дожать вышедших: иначе они
#: остаются зомби до конца жизни агента — своя сессия не делает процесс чужим,
#: он остаётся нашим ПРЯМЫМ потомком. Выметаем на следующем запуске.
_BACKGROUND_PROCS: set = set()


def _reap_finished_background() -> None:
    """Снять с учёта вышедших фоновых потомков.

    Прежняя редакция смотрела на `returncode` объекта asyncio — и это был
    мёртвый код: его выставляет наблюдатель ТОГО loop, на котором процесс
    создан, а loop хода к этому времени закрыт. Записи не удалялись никогда.
    У `Popen` состояние спрашивается синхронно и без всякого loop.
    """
    for proc in list(_BACKGROUND_PROCS):
        try:
            if proc.poll() is not None:
                _BACKGROUND_PROCS.discard(proc)
        except Exception:                      # noqa: BLE001 — учёт не критичен
            _BACKGROUND_PROCS.discard(proc)


# --- асинхронный запуск процесса (async-переезд, Задача 18) -----------------
#
# Прежняя реализация держала ПО ДВА потока на каждый вызов: pipe нельзя просто
# не читать — заполненный буфер заблокирует запись в дочернем процессе, а
# читать оба потока из одного треда нельзя, не рискуя дедлоком. asyncio решает
# ту же задачу двумя корутинами на одном loop.
#
# Семантика сохранена дословно: буфер ограничен, хвост дочитывается в никуда
# (иначе процесс встанет на записи), таймаут убивает процесс и отдаёт частичный
# буфер с ``return_code=None``.


class _Capture:
    """Захваченное из одного pipe, доступное ДО конца дренажа.

    Синхронный ``_drain_pipe`` публиковал буфер через колбэк ``on_captured``
    сразу по достижении лимита и не заставлял вызывающего ждать конца
    отбрасывания хвоста. Асинхронный двойник возвращал результат только
    через ``task.result()`` — то есть при отмене или таймауте дренажа всё
    захваченное терялось. Держим его в объекте, переживающем отмену таски.
    """

    __slots__ = ("stream", "acc")

    def __init__(self, capture_max_chars: int) -> None:
        #: Накопитель с ОГРАНИЧЕННОЙ памятью: настоящая голова, настоящий
        #: хвост, точный счётчик. Живёт ЗДЕСЬ, а не в локальной переменной
        #: корутины, чтобы вызывающий мог забрать прочитанное, НЕ отменяя
        #: дренаж: отмена оставляла трубу нечитаемой, и фоновый потомок
        #: вставал на write.
        self.acc = BoundedStreamAccumulator(capture_max_chars)
        self.stream: CapturedStream = self.acc.result()

    @property
    def text(self) -> str:
        return self.stream.text

    @property
    def hit_cap(self) -> bool:
        return self.stream.truncated

    def publish(self) -> None:
        """Снять снимок прочитанного к этому моменту. Идемпотентно.

        Раньше публикация была одноразовой: по достижении лимита буфер
        замораживался, потому что хвост всё равно отбрасывался. Теперь хвост
        НАСТОЯЩИЙ — накопитель держит его кольцом, — поэтому снимок берётся
        каждый раз заново и с каждым чтением становится точнее.
        """
        self.stream = self.acc.result()


async def _adrain_stream(stream, sink: "_Capture") -> None:
    """Прочитать поток ДО EOF в ограниченный накопитель ``sink``.

    `await stream.read(...)` — настоящая точка переключения: пока команда
    печатает, мы отдаём управление на каждом чанке. Это не вежливость, а
    свойство трубы: читать её обязательно, иначе заполненный буфер остановит
    запись в потомке, — и ровно поэтому уступка loop здесь неустранима.

    Чтение не прекращается по заполнении головы, и это не расточительство:
    хвост нужен настоящий, а счётчик — точный. Кольцевой хвост накопителя
    стоит O(1) на чанк.

    ``sink`` публикуется по ходу дела, поэтому отмена таски уже не отнимает
    захваченное: вызывающий получает то, что команда успела напечатать.
    """
    decoder = codecs.lookup("utf-8").incrementaldecoder(errors="replace")
    try:
        while True:
            chunk = await stream.read(_READ_CHUNK)
            if not chunk:
                break
            sink.acc.feed(decoder.decode(chunk))
            sink.publish()
    except asyncio.CancelledError:
        sink.publish()
        raise
    except Exception:
        sink.publish()
        return
    sink.acc.feed(decoder.decode(b"", final=True))
    sink.publish()


async def _astop_process(proc) -> None:
    """Остановить команду: сначала попросить, потом убить.

    `SIGTERM` → грация → `SIGKILL` вместо одного KILL. Разница не в том, умрёт
    ли команда — умрёт в обоих случаях, — а в том, что она успеет сделать перед
    смертью: снять `.git/index.lock`, дописать архив, закрыть транзакцию.
    KILL такого шанса не даёт вовсе, потому что не перехватывается.
    """
    if _terminate_quietly(proc):
        try:
            await _await_process_exit(proc, _KILL_GRACE_SEC)
            return                       # вышла сама, убивать некого
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
    _kill_quietly(proc)
    try:
        await _await_process_exit(proc, _KILL_EXIT_GRACE_SEC)
    except (asyncio.TimeoutError, asyncio.CancelledError):
        pass


async def _await_process_exit(proc, timeout: Optional[float]):
    """Дождаться ВЫХОДА процесса, а не EOF его pipe'ов.

    ``Process.wait()`` резолвится только когда закрыты все пайпы (asyncio
    будит ожидающих из ``_call_connection_lost``). Команда, оставившая
    фонового потомка с унаследованным stdout, держит pipe открытым — и такой
    wait выжигал весь таймаут, отдавая ложный ``timed_out`` и теряя вывод,
    который команда уже успела напечатать. Синхронный ``Popen.wait()``
    возвращался по выходу процесса независимо от пайпов; сохраняем это.

    ``proc.returncode`` выставляется в ``_process_exited`` — раньше закрытия
    пайпов и независимо от него.
    """
    deadline = None if timeout is None else time.monotonic() + float(timeout)
    delay = 0.001
    while proc.returncode is None:
        if deadline is not None and time.monotonic() >= deadline:
            raise asyncio.TimeoutError
        await asyncio.sleep(delay)
        # Растим паузу: короткие команды ловим почти мгновенно, длинные не
        # заставляют loop просыпаться сотни раз в секунду.
        delay = min(delay * 2, 0.05)
    return proc.returncode


async def _arun_bounded_process(
    args,
    *,
    shell: bool,
    cwd: Optional[str],
    timeout: Optional[int],
    env: Optional[Dict[str, str]],
    stdin_text: Optional[str],
    capture_max_chars: int,
) -> ExecResult:
    """Запустить процесс с ограниченным буфером, не занимая ни одного потока."""
    from gc_core.utils.win_console import preserve_console_mode

    stdin_arg = (
        asyncio.subprocess.PIPE if stdin_text is not None else asyncio.subprocess.DEVNULL
    )
    # Своя группа процессов. Без неё `proc.kill()` на таймауте убивал ТОЛЬКО
    # шелл: потомки `sh -c "a; b"` продолжали работать, унаследовав pipe'ы, и
    # переживали и таймаут, и отмену. С группой убиваем всю команду разом
    # (см. `_kill_quietly`). На Windows группы нет — там поведение прежнее.
    spawn_kwargs = {"start_new_session": True} if os.name != "nt" else {}

    with preserve_console_mode():
        if shell:
            proc = await asyncio.create_subprocess_shell(
                args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=stdin_arg, cwd=cwd, env=env, limit=_READ_CHUNK * 8,
                **spawn_kwargs,
            )
        else:
            proc = await asyncio.create_subprocess_exec(
                *args,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                stdin=stdin_arg, cwd=cwd, env=env, limit=_READ_CHUNK * 8,
                **spawn_kwargs,
            )

    # Читатели ЗАПУСКАЮТСЯ ПЕРВЫМИ, до записи в stdin, и это не стилистика.
    # Труба двусторонняя и обе стороны конечны: если stdin больше буфера трубы
    # с high-water (около 128 КиБ), а ребёнок до чтения stdin успевает выдать
    # больше ~192 КиБ (asyncio перестаёт читать на 2×limit), то родитель встаёт
    # в `drain()`, ребёнок — в `write()`, и это взаимная блокировка НАВСЕГДА:
    # таймаут тула стартует ниже, в `_await_process_exit`, и до этой фазы не
    # доходит. Достижимо из `skills_run_script` (аргументы уходят JSON'ом через
    # stdin) и `arun_python` с большим кодом.
    out_sink = _Capture(capture_max_chars)
    err_sink = _Capture(capture_max_chars)
    out_task = asyncio.ensure_future(_adrain_stream(proc.stdout, out_sink))
    err_task = asyncio.ensure_future(_adrain_stream(proc.stderr, err_sink))

    async def _feed_stdin() -> None:
        try:
            proc.stdin.write(stdin_text.encode("utf-8"))
            await proc.stdin.drain()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            try:
                proc.stdin.close()
            except Exception:
                pass

    # Запись держим под ТЕМ ЖЕ дедлайном, что и сам процесс: ребёнок, который
    # не читает свой stdin вовсе, иначе снова подвесил бы шаг без предохранителя.
    stdin_task = (
        asyncio.ensure_future(_feed_stdin())
        if stdin_text is not None and proc.stdin is not None
        else None
    )

    timed_out = False
    try:
        try:
            await _await_process_exit(proc, timeout)
            return_code = proc.returncode
        except asyncio.TimeoutError:
            timed_out = True
            return_code = None
            await _astop_process(proc)
        finally:
            # Ребёнок мёртв или дочитан — писать больше некому и незачем.
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()

        # Потомок вышел — читателям пора дойти до EOF. Ждём ОГРАНИЧЕННО: EOF
        # наступает, когда трубу закрыл ПОСЛЕДНИЙ писатель, а фоновый внук с
        # унаследованным дескриптором может держать её сколько угодно долго.
        await asyncio.wait({out_task, err_task}, timeout=_DRAIN_JOIN_TIMEOUT_SEC)
    except asyncio.CancelledError:
        # Вызывающего сняли (сервер отменил прогон, упал сосед по TaskGroup).
        await _astop_process(proc)
        raise
    finally:
        for task in (out_task, err_task):
            if not task.done():
                task.cancel()
        # Публикуем захваченное к этому моменту: даже на отмене вызывающий
        # получает то, что команда успела напечатать.
        out_sink.publish()
        err_sink.publish()
        _close_transport(proc)

    return ExecResult(
        return_code=return_code,
        timed_out=timed_out,
        stdout_stream=out_sink.stream,
        stderr_stream=err_sink.stream,
    )


def _close_transport(proc) -> None:
    """Закрыть транспорт подпроцесса явно.

    Без этого он жил до сборки мусора, и его ``__del__``, попав уже на
    закрытый loop, печатал ``RuntimeError: Event loop is closed``. Ошибку
    из ``__del__`` Python проглатывает, поэтому в наборе она была видна лишь
    как ``PytestUnraisableExceptionWarning`` — и её пролистывали.
    """
    transport = getattr(proc, "_transport", None)
    if transport is None:
        return
    try:
        transport.close()
    except Exception:
        pass


def _terminate_quietly(proc) -> bool:
    """Попросить команду завершиться самой: SIGTERM всей её группе.

    Ходит парой с `_kill_quietly`: сначала просим, через `_KILL_GRACE_SEC`
    убиваем. SIGKILL не перехватывается, поэтому без этой просьбы даже
    корректная команда не получает шанса прибраться за собой — и после таймаута
    остаются повисшие блокировки и недописанные файлы, разгребать которые
    придётся следующему ходу агента.

    ``False`` — послать было некому: ждать грацию незачем, идём убивать сразу.
    """
    pid = getattr(proc, "pid", None)
    if pid and os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            return True
        except (ProcessLookupError, PermissionError, OSError):
            return False
    try:
        proc.terminate()
        return True
    except Exception:
        return False


def _kill_quietly(proc) -> None:
    """Снять команду ЦЕЛИКОМ, а не только её шелл.

    `proc.kill()` бьёт по одному pid. Для `sh -c "a; b"` это шелл, а его
    потомки переживали и таймаут, и отмену — с унаследованными pipe'ами, то
    есть ещё и удерживая дескрипторы. Процесс запускается своей группой
    (`start_new_session=True`), поэтому здесь бьём по группе.
    """
    pid = getattr(proc, "pid", None)
    if pid and os.name != "nt":
        try:
            os.killpg(os.getpgid(pid), signal.SIGKILL)
            return
        except (ProcessLookupError, PermissionError, OSError):
            pass                      # группы уже нет либо она не наша
    try:
        proc.kill()
    except Exception:
        pass


# --- python-subprocess env allowlist (moved from run_python_code.py) ---
_CHILD_ENV_ALLOWLIST = frozenset(
    {
        "HOME",
        "LOGNAME",
        "USER",
        "USERNAME",
        "HOMEDRIVE",
        "HOMEPATH",
        "USERPROFILE",
        "SHELL",
        "TERM",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "LC_MESSAGES",
        "LC_NUMERIC",
        "LC_TIME",
        "TZ",
        "PATH",
        "PATHEXT",
        "TMPDIR",
        "TMP",
        "TEMP",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "REQUESTS_CA_BUNDLE",
        "CURL_CA_BUNDLE",
        "NODE_EXTRA_CA_CERTS",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "FTP_PROXY",
        "NO_PROXY",
        "http_proxy",
        "https_proxy",
        "all_proxy",
        "ftp_proxy",
        "no_proxy",
        "PYTHONUTF8",
        "PYTHONIOENCODING",
        "SYSTEMROOT",
        "WINDIR",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMDATA",
        "APPDATA",
        "LOCALAPPDATA",
        "PUBLIC",
    }
)


def _child_env_from_parent() -> Dict[str, str]:
    return {
        k: v
        for k, v in os.environ.items()
        if k in _CHILD_ENV_ALLOWLIST and isinstance(v, str)
    }


def _sanitize_env(env: Optional[Dict[str, Any]]) -> Dict[str, str]:
    base_env = {**_child_env_from_parent(), "PYTHONPATH": ""}
    if env is None:
        return base_env
    if not isinstance(env, dict):
        return base_env
    sanitized: Dict[str, str] = {}
    for k, v in env.items():
        if k is None:
            continue
        key = k if isinstance(k, str) else str(k)
        if v is None:
            sanitized[key] = ""
        elif isinstance(v, str):
            sanitized[key] = v
        else:
            sanitized[key] = str(v)
    merged = {**base_env, **sanitized}
    merged["PYTHONPATH"] = ""
    return merged
