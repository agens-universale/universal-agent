"""Файловая система как POSIX-команды поверх чужого exec.

Общая часть HarborBackend и RemoteShellBackend: stat/обход/append/copy —
это ``test``, ``stat -c``, ``find -printf``, ``cat``; содержимое файла ходит
через два примитива upload/download. Транспорт (как именно запустить
команду и перебросить байты) — дело подкласса, здесь его нет.

Требования к удалённой стороне: ``sh``, ``test``, ``stat`` (GNU ``-c``),
``find`` (GNU ``-printf``), ``readlink -f``, ``mkdir``, ``cp``, ``cat``,
``sha256sum``, ``head``, ``base64``; для фона ``bash``. Опционально ``setsid``.
"""

import base64
import posixpath
import shlex
import uuid
from abc import abstractmethod
from typing import Iterable, Optional, Sequence

from gc_core.backends.capabilities import parse_find_printf_entries
from gc_core.backends.protocol import (
    FIND_PRINTF_FORMAT,
    ExecResult,
    WalkOutputTooLargeError,
)
from gc_core.utils.file_text import (
    decode_text_best_effort,
    encode_text_for_append,
    encode_text_for_write,
)
from gc_core.utils.native_fs_search import find_prune_group, find_walk_head

#: Потолок на один вопрос «где мы» при сборке контекста. Со стороны ядра
#: дедлайна нет, и без него медленный контейнер задерживал бы сборку сколько
#: угодно; сам ответ приходит за миллисекунды.
CWD_PRIME_TIMEOUT_SEC = 20

#: Потолок на запуск фоновой команды. Сам запуск — это `exec`, который только
#: отцепляет команду и печатает pid, ответ приходит за доли секунды.
BACKGROUND_SPAWN_TIMEOUT_SEC = 30


def build_find_walk_command(
    path: str,
    *,
    prune_names: Sequence[str] = (),
    prune_dot_dirs: bool = False,
) -> str:
    """``find -H PATH -mindepth 1 [( prunes ) -printf FMT -prune -o] -printf FMT``.

    A pruned directory is printed BEFORE ``-prune``: the walk lists it (name
    search finds ``node_modules`` itself), only its subtree is cut.
    """
    argv = find_walk_head(path)
    group = find_prune_group(
        exclude_dir_names=prune_names, exclude_nested_dot_dirs=prune_dot_dirs
    )
    if group:
        argv.extend(group + ["-printf", FIND_PRINTF_FORMAT, "-prune", "-o"])
    argv.extend(["-printf", FIND_PRINTF_FORMAT])
    return " ".join(shlex.quote(a) for a in argv)


class PosixShellFsMixin:
    """Реализация FsBackend-методов ФС через три примитива подкласса.

    Подкласс задаёт ``walk_output_max_chars``, ставит ``_discovered_cwd = None``
    в ``__init__`` и реализует корутину ``aexec`` (нужна фону).
    """

    walk_output_max_chars: int
    _discovered_cwd: Optional[str]

    # --- примитивы транспорта ---
    @abstractmethod
    async def _afs_exec(
        self, command: str, *, timeout: Optional[int] = None
    ) -> ExecResult:
        """Сырой POSIX без профиля; потолок захвата — walk_output_max_chars.

        Только корутина: у удалённого бэкенда файловая операция — это сеть,
        синхронная обёртка на loop владельца самоблокировалась бы.
        """

    @abstractmethod
    async def _aupload_bytes(self, data: bytes, path: str) -> None:
        """Записать байты в ``path`` (родительские каталоги создать)."""

    @abstractmethod
    async def _adownload_bytes(self, path: str) -> bytes:
        """Прочитать файл ``path`` целиком."""

    # --- file read ---
    async def read(self, path: str) -> str:
        # Raw bytes via download, then same codec as local ``read_text_for_edit``.
        if not await self.exists(path):
            raise FileNotFoundError(path)
        text = decode_text_best_effort(await self.read_bytes(path))
        if text is not None:
            return text
        raise UnicodeDecodeError(
            "utf-8",
            b"",
            0,
            1,
            f"undecodable or binary file: {path}",
        )

    async def read_bytes(self, path: str) -> bytes:
        return await self._adownload_bytes(path)

    async def iter_lines(self, path: str) -> Iterable[str]:
        for line in (await self.read(path)).splitlines(keepends=True):
            yield line

    async def read_head_bytes(self, path: str, n: int) -> bytes:
        """Raw file prefix. ``exec`` stdout is text — pipe via base64 to avoid mangling."""
        n = max(0, int(n))
        if n == 0:
            return b""
        # GNU coreutils on the remote side (same assumption as ``stat -c`` / ``find -printf``).
        # Capture at walk ceiling: append samples 100KiB (~137k base64) > tool cap.
        # Do not pass ``-w 0``: Harbor's streamed exec reads stdout with
        # ``StreamReader`` readline (64 KiB). An unwrapped blob is one line and
        # raises LimitOverrunError before we see stdout. Default wrap is 76
        # columns; ``b64decode(validate=False)`` ignores the newlines.
        res = await self._afs_exec(f"head -c {n} {shlex.quote(path)} | base64")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"head failed: {path}")
        if res.stdout_hit_capture_cap:
            raise OSError(f"head output exceeded capture ceiling: {path}")
        try:
            return base64.b64decode(res.stdout or "", validate=False)
        except Exception as exc:
            raise OSError(f"base64 decode failed for head of {path}") from exc

    async def sha256_bytes(self, path: str) -> str:
        return await self.sha256(path)

    # --- stat ---
    async def exists(self, path: str) -> bool:
        return (
            await self._afs_exec(f"test -e {shlex.quote(path)}")
        ).return_code == 0

    async def lexists(self, path: str) -> bool:
        return (
            await self._afs_exec(
                f"test -e {shlex.quote(path)} || test -L {shlex.quote(path)}"
            )
        ).return_code == 0

    async def is_file(self, path: str) -> bool:
        return (
            await self._afs_exec(f"test -f {shlex.quote(path)}")
        ).return_code == 0

    async def is_dir(self, path: str) -> bool:
        return (
            await self._afs_exec(f"test -d {shlex.quote(path)}")
        ).return_code == 0

    async def is_symlink(self, path: str) -> bool:
        return (
            await self._afs_exec(f"test -L {shlex.quote(path)}")
        ).return_code == 0

    async def realpath(self, path: str) -> str:
        res = await self._afs_exec(f"readlink -f {shlex.quote(path)}")
        return res.stdout.strip() if res.return_code == 0 else path

    async def readable(self, path: str) -> bool:
        return True  # remote-root perspective (current harbor pack behavior)

    async def writable(self, path: str) -> bool:
        return True

    async def is_hidden(self, path: str) -> bool:
        return path.rstrip("/").rsplit("/", 1)[-1].startswith(".")

    async def getsize(self, path: str) -> int:
        res = await self._afs_exec(f"stat -c %s {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"stat failed: {path}")
        return int(res.stdout.strip())

    async def sha256(self, path: str) -> str:
        res = await self._afs_exec(f"sha256sum {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"sha256sum failed: {path}")
        return res.stdout.split()[0]

    async def stat(self, path: str) -> dict:
        res = await self._afs_exec(
            f"stat -c '%s|%Y|%a|%F' {shlex.quote(path)}"
        )
        if res.return_code != 0:
            raise FileNotFoundError(path)
        size, mtime, mode, kind = res.stdout.strip().split("|", 3)
        return {
            "size": int(size),
            "mtime": int(mtime),
            "mode": mode,
            "is_file": kind == "regular file",
            "is_dir": kind == "directory",
            "is_symlink": "symbolic link" in kind,
        }

    async def stat_info(
        self, path: str, follow_symlinks: bool = False
    ) -> dict:
        st = await self.stat(path)  # stat -c '%s|%Y|%a|%F' wrapper
        return {
            "size": st["size"],
            "mtime": st["mtime"],
            "mode": int(str(st["mode"]), 8),
            "is_file": st["is_file"],
            "is_dir": st["is_dir"],
            "is_symlink": st["is_symlink"],
        }

    # --- traverse ---
    async def walk_entries(
        self,
        path: str,
        *,
        prune_names: Sequence[str] = (),
        prune_dot_dirs: bool = False,
    ) -> list:
        """Recursive listing with sizes (``find -printf %s``) via the private
        traverse channel (50M chars). Pruned dirs are cut inside ``find`` so the
        channel is not spent on junk trees. Hit ceiling → fail-closed.
        """
        res = await self._afs_exec(
            build_find_walk_command(
                path, prune_names=prune_names, prune_dot_dirs=prune_dot_dirs
            )
        )
        if res.return_code != 0:
            raise FileNotFoundError(path)
        if res.stdout_hit_capture_cap:
            raise WalkOutputTooLargeError(
                path, ceiling_chars=self.walk_output_max_chars
            )
        return parse_find_printf_entries(res.stdout or "")

    async def walk(self, path: str) -> list:
        """Recursive listing under `path` as [(relpath, is_dir)] (relpath excludes path)."""
        return [(e.rel, e.is_dir) for e in await self.walk_entries(path)]

    async def listdir(self, path: str) -> list:
        """Immediate children via private traverse channel (same ceiling as walk)."""
        # Тот же общий рецепт, что у обхода: без -H find не идёт по ссылке-аргументу,
        # и каталог-ссылка выглядел пустым (пустой stdout при коде возврата 0).
        head = " ".join(shlex.quote(a) for a in find_walk_head(path))
        res = await self._afs_exec(f"{head} -maxdepth 1 -printf '%f\\t%y\\n'")
        if res.return_code != 0:
            raise FileNotFoundError(path)
        if res.stdout_hit_capture_cap:
            raise WalkOutputTooLargeError(
                path, ceiling_chars=self.walk_output_max_chars
            )
        out = []
        for line in (res.stdout or "").splitlines():
            if not line.strip():
                continue
            name, _, kind = line.partition("\t")
            out.append((name, kind[:1] == "d" if kind else False))
        return sorted(out)

    # --- path semantics ---
    def normpath(self, path: str) -> str:
        # find печатает пути префиксом вызова, prune сравнивает строки — «//» и
        # «/./» в префиксе молча ломали исключения. Удалённые пути всегда POSIX.
        return posixpath.normpath(path) if path else path

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    def isabs(self, path: str) -> bool:
        """Remote paths are POSIX whatever the host is."""
        return path.startswith("/")

    # --- cwd ---
    def default_cwd(self) -> Optional[str]:
        # Рабочая папка удалённой стороны: узнаётся один раз при сборке
        # контекста (`aprime_default_cwd`), до того — None.
        return self._discovered_cwd

    async def aprime_default_cwd(self) -> Optional[str]:
        """Узнать рабочую папку удалённой стороны один раз — сырым каналом.

        Синхронных сборщиков рабочей области восемь (включая конструктор
        ``State``), каскадом в корутины их не переводим: как и пробы
        возможностей, значение прогревается один раз при сборке контекста и
        дальше отдаётся из кэша.
        """
        if self._discovered_cwd is None:
            try:
                # Таймаут обязателен: без него сборка контекста ждала бы медленную
                # удалённую сторону сколько угодно — со стороны ядра дедлайна нет.
                res = await self._afs_exec(
                    "pwd", timeout=CWD_PRIME_TIMEOUT_SEC
                )
            except Exception:  # окружение может быть недоступно
                return None
            lines = (res.stdout or "").strip().splitlines()
            if res.return_code == 0 and lines and lines[0].startswith("/"):
                self._discovered_cwd = lines[0]
        return self._discovered_cwd

    # --- file write/edit ---
    async def write(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None:
        # Allowlist + strict encode on host → bytes upload (same path as write_bytes).
        await self.write_bytes(path, encode_text_for_write(content, encoding))

    async def write_bytes(self, path: str, data: bytes) -> None:
        await self._aupload_bytes(data, path)

    async def append(
        self, path: str, content: str, encoding: str = "utf-8"
    ) -> None:
        # Upload only the new chunk, then shell-append (no full-file download).
        # Append payload WITHOUT a leading BOM: a second BOM in the middle of an
        # existing utf-16 file decodes as U+FEFF (ZERO WIDTH NO-BREAK SPACE).
        data = encode_text_for_append(content, encoding)
        remote_tmp = f"{path}.gc-append-tmp.{uuid.uuid4().hex}"
        try:
            await self._aupload_bytes(data, remote_tmp)
            res = await self._afs_exec(
                f"cat {shlex.quote(remote_tmp)} >> {shlex.quote(path)}; rm -f {shlex.quote(remote_tmp)}"
            )
            if res.return_code != 0:
                raise OSError(res.stderr.strip() or f"append failed: {path}")
        finally:
            # Best-effort: upload/exec may leave remote_tmp if the cat+rm line never ran.
            try:
                await self._afs_exec(f"rm -f {shlex.quote(remote_tmp)}")
            except Exception:
                pass

    async def copy(self, src: str, dst: str) -> None:
        res = await self._afs_exec(f"cp {shlex.quote(src)} {shlex.quote(dst)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"cp failed: {src} -> {dst}")

    async def makedirs(self, path: str) -> None:
        res = await self._afs_exec(f"mkdir -p {shlex.quote(path)}")
        if res.return_code != 0:
            raise OSError(res.stderr.strip() or f"mkdir failed: {path}")

    # --- background ---
    async def aspawn_background(
        self,
        command: str,
        *,
        cwd: Optional[str] = None,
        env: Optional[dict] = None,
    ) -> dict:
        """Запустить команду на удалённой стороне фоном и отдать её pid.

        `&` в обычной команде не спасает — трубы вызова закрываются вместе с
        ним. Здесь команда отцепляется на самой удалённой стороне: `setsid`
        уводит её из сессии exec, `nohup` снимает SIGHUP, а все три потока
        смотрят в /dev/null — ни одной трубы к каналу, который держит exec.
        Вывод не сохраняется, как и у локального бэкенда.

        Канал модельный (`aexec`): инициализация окружения (bashrc), `env` и
        `cwd` те же, что у переднего плана, и внутренний `bash -c` их
        наследует. В неинтерактивном bash фоновое задание не лидер группы,
        поэтому `setsid` не форкается, и `$!` — pid самой команды после цепочки
        exec'ов setsid → nohup → bash. Где `setsid` нет, остаётся `nohup`:
        exec без терминала SIGHUP при выходе не рассылает.
        """
        quoted = shlex.quote(command)
        detached = (
            "if command -v setsid >/dev/null 2>&1; then "
            f"setsid nohup bash -c {quoted} </dev/null >/dev/null 2>&1 & "
            f"else nohup bash -c {quoted} </dev/null >/dev/null 2>&1 & fi; "
            "echo $!"
        )
        res = await self.aexec(
            detached,
            cwd=cwd,
            env=env,
            timeout=BACKGROUND_SPAWN_TIMEOUT_SEC,
        )
        if getattr(res, "timed_out", False):
            # Не «pid не сообщён»: оболочка не вернулась вовсе, а команда могла и
            # стартовать. Перезапуск вслепую поднял бы второй экземпляр сервиса.
            raise RuntimeError(
                f"background command start timed out after "
                f"{BACKGROUND_SPAWN_TIMEOUT_SEC} s: the container shell did not "
                "return, the command may or may not be running"
            )
        # Последняя строка: профиль оболочки печатает в stdout раньше нас.
        lines = (getattr(res, "stdout", "") or "").strip().splitlines()
        try:
            return {"pid": int(lines[-1])}
        except (IndexError, ValueError):
            raise RuntimeError(
                "background command did not report its pid: "
                f"exit={getattr(res, 'return_code', None)} "
                f"stderr={(getattr(res, 'stderr', '') or '').strip()[:200]!r}"
            ) from None
