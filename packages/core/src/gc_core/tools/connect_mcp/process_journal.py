"""Журнал процессов MCP-серверов: учёт запусков, аварийное гашение, сироты.

Каждый процесс сервера под сервисом запускается через пускатель
(``spawn_launcher.py``), который пишет в каталог журнала файл запуска
``<pid>.json`` = ``{"pid", "pgid", "mark", "server"}`` и при сбое ``exec``
— ещё ``error`` и ``detail``. Каталог — один на журнал (сервис):
``<gc_state>/mcp/processes/<boot_id>-<pid>-<метка хоста>-<случайное>/``,
в нём ``owner.json`` = ``{"pid", "mark", "node", "boot_id"}`` хозяина.
Хозяин разбирается только по ``owner.json``, имя каталога — для людей.

Правило всех сигналов: процесс доказан своим — жив, не зомби, метка
личности совпала с записью, а групповой сигнал получает только лидер
группы (``pgid == pid``). Несовпавшая запись не трогается.

Linux и macOS — полноценно; на других ОС пускателя нет (команда идёт как
есть), журнал пуст, уборка ничего не делает.

Журнал — не данные агента, а учёт процессов хоста: пишется мимо хранилищ.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import platform
import re
import shutil
import signal
import sys
import tempfile
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path

from gc_core.config_modules.env_vars import gc_paths
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.process_identity import boot_id, process_start_mark

logger = logging.getLogger(__name__)

_LAUNCHER = Path(__file__).with_name("spawn_launcher.py")
_OWNER_FILE = "owner.json"
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
#: Шаг опроса, пока лидеры групп выходят после SIGTERM.
_POLL_STEP_SEC = 0.05
#: Ожидание сирот после SIGTERM до SIGKILL.
_ORPHAN_WAIT_SEC = 2.0


def journal_root() -> Path:
    """Корень журналов всех хостов этой машины."""
    return gc_paths()["state"] / "mcp" / "processes"


@dataclass(frozen=True)
class ProcessRecord:
    """Файл запуска пускателя; ``error`` есть только при сбое ``exec``."""

    pid: int
    pgid: int
    mark: str | None
    server: str
    error: str | None
    detail: str | None
    path: Path


class ProcessJournal:
    """Журнал процессов одного сервиса.

    Каталог и ``owner.json`` создаются при первом ``launch_argv``.
    ``kill_all`` и ``prune`` замков не берут: список читается с диска,
    поэтому их можно звать из обработчика сигнала и из чужого потока,
    одновременно друг с другом.
    """

    def __init__(self, root: Path | None = None) -> None:
        self._root = root if root is not None else journal_root()
        self._dir: Path | None = None
        self._lock = threading.Lock()

    def launch_argv(
        self, command: str, args: list[str], server_key: str
    ) -> tuple[str, list[str]]:
        """Команда запуска сервера через пускатель.

        Нет учёта на этой ОС или каталог не создать — команда как есть.
        """
        if not _tracking_supported():
            return command, args
        directory = self._ensure_dir()
        if directory is None:
            return command, args
        launcher = [str(_LAUNCHER), str(directory), server_key, "--"]
        return sys.executable, ["-I", *launcher, command, *args]

    def records(self) -> list[ProcessRecord]:
        """Целые файлы запусков, от старых к новым; битые пропускаются."""
        directory = self._dir
        return _read_records(directory) if directory is not None else []

    def launch_error(self, server_key: str) -> ProcessRecord | None:
        """Самая свежая запись сервера со сбоем ``exec``."""
        failed = [
            r
            for r in self.records()
            if r.server == server_key and r.error is not None
        ]
        return failed[-1] if failed else None

    def prune(self) -> None:
        """Снять файлы процессов, которых уже нет (или их не узнать)."""
        for record in self.records():
            if not _is_same_process(record):
                record.path.unlink(missing_ok=True)

    def kill_all(self, *, wait_sec: float = 2.0) -> None:
        """Погасить все доказанно свои группы: SIGTERM, ожидание, SIGKILL.

        После круга журнал читается ещё раз: запуск, начатый до вызова,
        мог записаться во время ожидания — он получает свой круг.
        """
        first = _provably_ours(self.records())
        _terminate_groups(first, wait_sec)
        handled = {(r.pid, r.mark) for r in first}
        late = [
            r
            for r in _provably_ours(self.records())
            if (r.pid, r.mark) not in handled
        ]
        _terminate_groups(late, wait_sec)

    def remove(self) -> None:
        """Удалить каталог журнала; следующий запуск заведёт новый."""
        with self._lock:
            directory, self._dir = self._dir, None
        if directory is not None:
            shutil.rmtree(directory, ignore_errors=True)

    def _ensure_dir(self) -> Path | None:
        with self._lock:
            if self._dir is None:
                self._dir = _create_dir(self._root)
            return self._dir


def sweep_orphans(
    root: Path | None = None, *, wait_sec: float = _ORPHAN_WAIT_SEC
) -> int:
    """Убрать журналы мёртвых хозяев этого узла; вернуть число SIGTERM.

    Каталог другой загрузки удаляется без сигналов. Журнал другого узла
    и живого хозяина не трогается. У мёртвого хозяина доказанно своим
    группам — SIGTERM; через ``wait_sec`` тем, чей лидер жив с той же
    меткой, — SIGKILL; затем каталог удаляется. Группа мёртвого лидера
    не трогается: её номер мог достаться чужому.
    """
    if not _tracking_supported():
        return 0
    base = root if root is not None else journal_root()
    doomed: list[Path] = []
    targets: list[ProcessRecord] = []
    for directory in _owner_dirs(base):
        verdict = _orphan_verdict(directory)
        if verdict is None:
            continue
        doomed.append(directory)
        if verdict:
            targets.extend(_provably_ours(_read_records(directory)))
    signalled = [r for r in targets if _signal_group(r.pgid, signal.SIGTERM)]
    _await_exit(signalled, wait_sec)
    for record in signalled:
        if _is_same_process(record):
            _signal_group(record.pgid, signal.SIGKILL)
    for directory in doomed:
        shutil.rmtree(directory, ignore_errors=True)
    return len(signalled)


def _tracking_supported() -> bool:
    return sys.platform.startswith("linux") or sys.platform == "darwin"


def _alnum(text: str) -> str:
    return _NOT_ALNUM.sub("", text) or "0"


def _create_dir(root: Path) -> Path | None:
    """Каталог журнала с ``owner.json``; не вышло — ``None`` и строка в лог."""
    mark = process_start_mark(os.getpid())
    boot = boot_id()
    parts = (boot, str(os.getpid()), mark or "", uuid.uuid4().hex[:8])
    directory = root / "-".join(_alnum(part) for part in parts)
    owner = {
        "pid": os.getpid(),
        "mark": mark,
        "node": platform.node(),
        "boot_id": boot,
    }
    try:
        directory.mkdir(parents=True)
        _write_json(directory / _OWNER_FILE, owner)
    except OSError as exc:
        logger.warning("[MCP] журнал процессов не создан: %s", exc)
        shutil.rmtree(directory, ignore_errors=True)
        return None
    return directory


def _write_json(path: Path, payload: dict) -> None:
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        atomic_replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


def _read_json(path: Path) -> object:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def _read_records(directory: Path) -> list[ProcessRecord]:
    ranked: list[tuple[int, str, ProcessRecord]] = []
    try:
        entries = list(directory.glob("*.json"))
    except OSError:
        return []
    for path in entries:
        if not path.stem.isdigit():
            continue
        record = _parse_record(path, _read_json(path))
        if record is None:
            continue
        with contextlib.suppress(OSError):
            ranked.append((path.stat().st_mtime_ns, path.name, record))
    ranked.sort(key=lambda item: item[:2])
    return [record for _, _, record in ranked]


def _parse_record(path: Path, data: object) -> ProcessRecord | None:
    if not isinstance(data, dict):
        return None
    pid, pgid, server = data.get("pid"), data.get("pgid"), data.get("server")
    if not (_is_int(pid) and _is_int(pgid) and isinstance(server, str)):
        return None
    return ProcessRecord(
        pid=pid,
        pgid=pgid,
        mark=_str_or_none(data.get("mark")),
        server=server,
        error=_str_or_none(data.get("error")),
        detail=_str_or_none(data.get("detail")),
        path=path,
    )


def _is_int(value: object) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str_or_none(value: object) -> str | None:
    return value if isinstance(value, str) else None


def _is_same_process(record: ProcessRecord) -> bool:
    """Процесс записи жив, не зомби, и метка та же."""
    return record.mark is not None and (
        process_start_mark(record.pid) == record.mark
    )


def _provably_ours(records: list[ProcessRecord]) -> list[ProcessRecord]:
    """Лидеры своих групп с совпавшей меткой — им можно групповой сигнал.

    Своя группа хоста исключена на случай подделанной записи.
    """
    own_group = os.getpgrp()
    return [
        r
        for r in records
        if r.pgid == r.pid and r.pgid != own_group and _is_same_process(r)
    ]


def _signal_group(pgid: int, sig: int) -> bool:
    """Сигнал группе; ``False`` — группы нет или сигнал не дошёл.

    ``pgid`` ≤ 1 отвергается: ``killpg(0, …)`` бьёт свою группу, а
    ``killpg(-1, …)`` на macOS становится ``kill(1, …)``.
    """
    if pgid <= 1:
        return False
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    except PermissionError as exc:
        # macOS отвечает EPERM группе, где остались одни зомби.
        logger.debug("[MCP] сигнал группе %s не дошёл: %s", pgid, exc)
        return False
    return True


def _await_exit(records: list[ProcessRecord], wait_sec: float) -> None:
    """Ждать до ``wait_sec``, пока не выйдут лидеры всех групп.

    Зомби считается вышедшим: лидера, которого хозяин ещё не пожал, по
    пустоте группы не дождаться.
    """
    deadline = time.monotonic() + wait_sec
    while any(_is_same_process(r) for r in records):
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(_POLL_STEP_SEC, left))


def _terminate_groups(records: list[ProcessRecord], wait_sec: float) -> None:
    """SIGTERM группам, ожидание лидеров, SIGKILL каждой группе.

    SIGKILL уходит и группе вышедшего лидера: глухой к SIGTERM потомок
    послушного лидера (``node`` и Chromium) иначе переживёт хоста. Пока в
    группе есть процессы, её номер никому не достанется.
    """
    if not records:
        return
    for record in records:
        _signal_group(record.pgid, signal.SIGTERM)
    _await_exit(records, wait_sec)
    for record in records:
        _signal_group(record.pgid, signal.SIGKILL)


def _owner_dirs(base: Path) -> list[Path]:
    try:
        return [entry for entry in base.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _orphan_verdict(directory: Path) -> bool | None:
    """Судьба каталога: ``None`` — не трогать, ``False`` — удалить без
    сигналов (другая загрузка), ``True`` — хозяин мёртв, гасить сирот."""
    owner = _read_json(directory / _OWNER_FILE)
    if not isinstance(owner, dict) or owner.get("node") != platform.node():
        return None
    if owner.get("boot_id") != boot_id():
        return False
    pid = owner.get("pid")
    if not _is_int(pid):
        return None
    current = process_start_mark(pid)
    mark = _str_or_none(owner.get("mark"))
    alive = current is not None and (mark is None or current == mark)
    return None if alive else True


__all__ = [
    "ProcessJournal",
    "ProcessRecord",
    "journal_root",
    "sweep_orphans",
]
