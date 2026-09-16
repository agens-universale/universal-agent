"""Host implementation of FsBackend (os / open() / subprocess); the host-I/O
details that used to live inline in the tools are concentrated here."""
import asyncio
import signal
import codecs
import hashlib
import os
import stat as stat_module
import tempfile
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

    def default_cwd(self) -> Optional[str]:
        return os.getcwd()


# --- bounded process runner (spec D1/D2/D6/D9) ---
# Потоки stdout/stderr читаются параллельно (thread-per-pipe), иначе большой
# вывод в один pipe заблокирует другой (deadlock). Буфер ≤ capture_max_chars на
# поток; после cap — drain & discard (НЕ kill). Без записи на диск.

_READ_CHUNK = 8192
# Discard после cap — fire-and-forget: данные уже в sink. Короткий join, чтобы
# не копить зомби-треды на типичном хвосте; если discard долгий — уходим с
# captured, reader дочитает в фоне как daemon (не блокирует process exit).
_DRAIN_JOIN_TIMEOUT_SEC = 2

#: Сколько ждать выхода уже убитого потомка, прежде чем отпускать дренаж.
#: Сигнал послан, речь о миллисекундах; потолок здесь — от зависшего в
#: непрерываемом системном вызове процесса, а не от нормального случая.
_KILL_EXIT_GRACE_SEC = 1.0

#: Шаг опроса файла захвата. У файла нет EOF, как у трубы: чтение возвращает
#: пусто и когда писатель молчит, и когда его уже нет, поэтому конец задаёт
#: хозяин процесса. 30 мс незаметны в выводе команды и не будят loop без дела.
_FILE_POLL_SEC = 0.03


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


def _open_capture_file() -> "tuple[int, int]":
    """Пара дескрипторов на один временный файл: (для записи, для чтения).

    Потомку отдаётся дескриптор ЗАПИСИ, и это главное свойство всей схемы:
    ни конец хода, ни выход процесса агента не закрывают ему stdout — он
    пишет в файл, а не в нашу трубу. Труба закрывалась вместе с loop хода, и
    фоновый потомок (`npm run dev &`, `uvicorn`) умирал на первой записи.

    На POSIX файл сразу удаляется из каталога: имя никому не нужно, а место
    освободится, когда закроется последний дескриптор — то есть когда умрёт
    последний писатель. Убирать за собой не требуется, и фоновому внуку это
    не мешает. На Windows удалить открытый файл нельзя, поэтому там имя
    остаётся и файл удаляется при закрытии (`O_TEMPORARY`).
    """
    flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
    if os.name == "nt":
        flags |= getattr(os, "O_TEMPORARY", 0) | getattr(os, "O_NOINHERIT", 0)
    fd, path = tempfile.mkstemp(prefix="gc-exec-")
    os.close(fd)
    os.unlink(path)
    write_fd = os.open(path, flags, 0o600)
    read_fd = os.open(path, os.O_RDONLY)
    if os.name != "nt":
        os.unlink(path)
    return write_fd, read_fd


async def _adrain_file(read_fd: int, sink: "_Capture", *, stop: "asyncio.Event") -> None:
    """Читать файл захвата по мере роста, пока не попросят остановиться.

    У файла нет EOF в смысле трубы: `read` возвращает пусто и когда писателей
    больше нет, и когда они просто молчат. Поэтому конец определяет не чтение,
    а `stop` — его ставит хозяин процесса, уже зная код возврата. После этого
    дочитываем остаток: то, что потомок успел выписать перед выходом.
    """
    decoder = codecs.lookup("utf-8").incrementaldecoder(errors="replace")
    try:
        while True:
            chunk = os.read(read_fd, _READ_CHUNK)
            if chunk:
                sink.acc.feed(decoder.decode(chunk))
                sink.publish()
                continue
            if stop.is_set():
                break
            await asyncio.sleep(_FILE_POLL_SEC)
    except (asyncio.CancelledError, OSError):
        sink.publish()
        raise
    sink.publish()


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

    # stdout/stderr — ФАЙЛЫ, а не трубы, и это лечит целый класс поломок.
    # Потомок и все его внуки наследуют дескриптор файла: ни конец хода (в TUI
    # каждый ход — свой `asyncio.run`), ни выход всего процесса агента (gc-cli,
    # трайл Harbor) им больше не мешают. С трубой закрытие читающего конца
    # роняло фоновый `npm run dev` на первой же записи, а непрочитанная труба
    # вставала на 64 КиБ. Заодно исчезла вся машинерия дренажа и её
    # платформенная развилка.
    out_write, out_read = _open_capture_file()
    err_write, err_read = _open_capture_file()
    try:
        with preserve_console_mode():
            if shell:
                proc = await asyncio.create_subprocess_shell(
                    args,
                    stdout=out_write, stderr=err_write,
                    stdin=stdin_arg, cwd=cwd, env=env,
                    **spawn_kwargs,
                )
            else:
                proc = await asyncio.create_subprocess_exec(
                    *args,
                    stdout=out_write, stderr=err_write,
                    stdin=stdin_arg, cwd=cwd, env=env,
                    **spawn_kwargs,
                )
    finally:
        # Свою копию дескриптора записи закрываем сразу: у потомка она своя, а
        # лишняя здесь держала бы файл живым после смерти всех писателей.
        for fd in (out_write, err_write):
            try:
                os.close(fd)
            except OSError:
                pass

    # Взаимной блокировки stdin↔stdout больше нет: в файл потомок пишет не
    # останавливаясь, сколько бы мы ни читали. Раньше здесь был обязательный
    # порядок «читатели до записи в stdin» — с трубами `skills_run_script` с
    # большим JSON на stdin мог встать намертво.
    out_sink = _Capture(capture_max_chars)
    err_sink = _Capture(capture_max_chars)
    #: Конец чтения задаём мы: у файла нет EOF, по которому его видно у трубы.
    stop = asyncio.Event()
    out_task = asyncio.ensure_future(_adrain_file(out_read, out_sink, stop=stop))
    err_task = asyncio.ensure_future(_adrain_file(err_read, err_sink, stop=stop))

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
            _kill_quietly(proc)
            try:
                await _await_process_exit(proc, 5)
            except Exception:
                pass
        finally:
            # Ребёнок мёртв или дочитан — писать больше некому и незачем.
            if stdin_task is not None and not stdin_task.done():
                stdin_task.cancel()

        # Потомок вышел — читателям пора дочитать остаток и закончить. Ждём
        # ограниченно: хвост уже лежит в файле, и долгое дочитывание не должно
        # удерживать tool-call.
        stop.set()
        await asyncio.wait({out_task, err_task}, timeout=_DRAIN_JOIN_TIMEOUT_SEC)
    except asyncio.CancelledError:
        # Вызывающего сняли (сервер отменил прогон, упал сосед по TaskGroup).
        # Процесс убиваем, читателей останавливаем; фоновым внукам это не
        # мешает — их stdout остался файлом, а не нашей трубой.
        _kill_quietly(proc)
        stop.set()
        try:
            await _await_process_exit(proc, _KILL_EXIT_GRACE_SEC)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            pass
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
        for fd in (out_read, err_read):
            try:
                os.close(fd)
            except OSError:
                pass

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
