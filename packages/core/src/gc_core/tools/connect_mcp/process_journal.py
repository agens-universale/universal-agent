"""Журнал процессов MCP-серверов: учёт запусков, аварийное гашение, сироты.

Каждый процесс сервера под сервисом запускается через пускатель
(``spawn_launcher.py``), который пишет в каталог журнала файл запуска
``<pid>.json`` = ``{"pid", "pgid", "mark", "server"}`` и при сбое ``exec``
— ещё ``error`` и ``detail``. Каталог — один на журнал (сервис):
``<gc_state>/mcp/processes/<boot_id>-<pid>-<метка хоста>-<случайное>/``,
в нём ``owner.json`` = ``{"pid", "mark", "node", "boot_id"}`` хозяина.
Хозяин разбирается только по ``owner.json``, имя каталога — для людей.
Каталоги ``0700``, файлы ``0600``; файлы чужого пользователя не читаются.

Правило всех сигналов: процесс доказан своим — метка личности лидера
совпала с записью, а групповой сигнал получает только лидер группы
(``pgid == pid``). Несовпавшая запись не трогается. Непожатый зомби-лидер
своим считается: он хранит то же время старта и держит pid и номер
группы, пока его не пожмут, поэтому сигнал его группе безопасен (так
гасится глухой потомок вышедшего лидера). В ожидании выхода зомби —
вышедший.

Между сверкой метки и сигналом есть окно. Оно безопасно, пока в группе
есть хоть один процесс (зомби тоже): номер группы занят и никому не
достанется. Опасен только полный оборот pid за время ожидания (≤ 2 с)
после того, как группа опустела целиком, — допущение спеки §5.12.

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
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from gc_core.config_modules.env_vars import gc_paths
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.process_identity import (
    boot_id,
    process_start_mark,
    process_start_states,
)

logger = logging.getLogger(__name__)

_LAUNCHER = Path(__file__).with_name("spawn_launcher.py")
_OWNER_FILE = "owner.json"
_NOT_ALNUM = re.compile(r"[^A-Za-z0-9]")
#: Шаг опроса, пока лидеры групп выходят после SIGTERM.
_POLL_STEP_SEC = 0.05
#: Ожидание сирот после SIGTERM до SIGKILL.
_ORPHAN_WAIT_SEC = 2.0

_States = dict[int, tuple[str, bool]]


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
        return sys.executable, ["-I", "-S", *launcher, command, *args]

    def records(self) -> list[ProcessRecord]:
        """Целые свои файлы запусков, от старых к новым; битые пропускаются."""
        directory = self._dir
        return _read_records(directory) if directory is not None else []

    def launch_error(self, server_key: str) -> ProcessRecord | None:
        """Самая свежая запись ключа, если в ней сбой ``exec``.

        Удачный запуск после сбоя прежнюю причину гасит.
        """
        own = [r for r in self.records() if r.server == server_key]
        if own and own[-1].error is not None:
            return own[-1]
        return None

    def prune(self) -> None:
        """Снять файлы процессов, которых уже нет (или их не узнать).

        Остаются записи со сбоем ``exec`` (их читает ``launch_error``,
        уходят они с каталогом) и записи непожатого зомби-лидера: пока он
        держит группу, ``kill_all`` гасит по ней глухих потомков.
        """
        records = self.records()
        states = _states_of(records)
        for record in records:
            if record.error is None and not _matches(record, states):
                record.path.unlink(missing_ok=True)

    def kill_all(self, *, wait_sec: float = 2.0) -> None:
        """Погасить все доказанно свои группы: SIGTERM, ожидание, SIGKILL.

        После круга журнал читается ещё раз: запуск, начатый до вызова,
        мог записаться во время ожидания — он получает свой круг. Срок
        ``wait_sec`` общий на оба круга.
        """
        deadline = time.monotonic() + wait_sec
        first = _provably_ours(self.records())
        _terminate_groups(first, deadline)
        handled = {(r.pid, r.mark) for r in first}
        late = [
            r
            for r in _provably_ours(self.records())
            if (r.pid, r.mark) not in handled
        ]
        _terminate_groups(late, deadline)

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

    Журнал другого узла и живого хозяина не трогается. Мёртвый хозяин
    другой загрузки — каталог удаляется без сигналов. Мёртвый хозяин этой
    загрузки: доказанно своим группам — SIGTERM; через ``wait_sec`` тем,
    чей лидер всё ещё с той же меткой (зомби тоже), — SIGKILL; затем
    каталог удаляется. Группа пожатого лидера не трогается: её номер мог
    достаться чужому.

    Две одновременные уборки могут обе послать SIGTERM одной сироте и обе
    её посчитать: число тогда завышено, сигналы безопасны.
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
    _await_exit(signalled, time.monotonic() + wait_sec)
    states = _states_of(signalled)
    for record in signalled:
        if _matches(record, states):
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
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        directory.mkdir(mode=0o700)
        _write_json(directory / _OWNER_FILE, owner)
    except OSError as exc:
        logger.warning("[MCP] журнал процессов не создан: %s", exc)
        shutil.rmtree(directory, ignore_errors=True)
        return None
    return directory


def _write_json(path: Path, payload: dict) -> None:
    """Файл ``0600`` (``mkstemp``) и атомарная замена."""
    fd, staged = tempfile.mkstemp(dir=path.parent, prefix=".", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        atomic_replace(staged, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(staged)
        raise


def _read_own_json(path: Path) -> tuple[object, int] | None:
    """(данные, mtime_ns) файла своего пользователя; иначе ``None``."""
    try:
        info = path.stat()
        if info.st_uid != os.getuid():
            return None
        return json.loads(path.read_text(encoding="utf-8")), info.st_mtime_ns
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
        found = _read_own_json(path)
        record = _parse_record(path, found[0]) if found else None
        if found and record is not None:
            ranked.append((found[1], path.name, record))
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


def _states_of(records: Iterable[ProcessRecord]) -> _States:
    return process_start_states(r.pid for r in records)


def _matches(record: ProcessRecord, states: _States) -> bool:
    """Процесс записи тот же (жив или непожатый зомби)."""
    found = states.get(record.pid)
    return (
        record.mark is not None
        and found is not None
        and (found[0] == record.mark)
    )


def _running(record: ProcessRecord, states: _States) -> bool:
    """Процесс записи тот же и ещё не вышел (не зомби)."""
    return _matches(record, states) and not states[record.pid][1]


def _provably_ours(records: list[ProcessRecord]) -> list[ProcessRecord]:
    """Лидеры своих групп с совпавшей меткой — им можно групповой сигнал.

    Своя группа хоста исключена на случай подделанной записи.
    """
    own_group = os.getpgrp()
    states = _states_of(records)
    return [
        r
        for r in records
        if r.pgid == r.pid and r.pgid != own_group and _matches(r, states)
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


def _await_exit(records: list[ProcessRecord], deadline: float) -> None:
    """Ждать до ``deadline`` (монотонные часы), пока выйдут все лидеры.

    Один опрос ОС на шаг. Зомби считается вышедшим: лидера, которого
    хозяин ещё не пожал, по пустоте группы не дождаться.
    """
    while records:
        states = _states_of(records)
        if not any(_running(r, states) for r in records):
            return
        left = deadline - time.monotonic()
        if left <= 0:
            return
        time.sleep(min(_POLL_STEP_SEC, left))


def _terminate_groups(records: list[ProcessRecord], deadline: float) -> None:
    """SIGTERM группам, ожидание дошедших, SIGKILL каждой группе.

    SIGKILL уходит и группе вышедшего лидера: глухой к SIGTERM потомок
    послушного лидера (``node`` и Chromium) иначе переживёт хоста. Пока в
    группе есть процессы, её номер никому не достанется. Ждутся только
    группы, которым SIGTERM дошёл: EPERM не тянет весь срок.
    """
    if not records:
        return
    signalled = [r for r in records if _signal_group(r.pgid, signal.SIGTERM)]
    _await_exit(signalled, deadline)
    for record in records:
        _signal_group(record.pgid, signal.SIGKILL)


def _owner_dirs(base: Path) -> list[Path]:
    try:
        return [entry for entry in base.iterdir() if entry.is_dir()]
    except OSError:
        return []


def _orphan_verdict(directory: Path) -> bool | None:
    """Судьба каталога: ``None`` — не трогать, ``False`` — удалить без
    сигналов (другая загрузка), ``True`` — хозяин мёртв, гасить сирот.

    Живой хозяин проверяется до загрузки: сбой ``boot_id()`` (``"0"``)
    не должен удалить журнал живого соседа.
    """
    found = _read_own_json(directory / _OWNER_FILE)
    owner = found[0] if found else None
    if not isinstance(owner, dict) or owner.get("node") != platform.node():
        return None
    pid = owner.get("pid")
    if not _is_int(pid) or _owner_alive(pid, owner.get("mark")):
        return None
    return owner.get("boot_id") == boot_id()


def _owner_alive(pid: int, mark: object) -> bool:
    """Хозяин жив: метка совпала; без метки в ``owner.json`` — pid жив."""
    current = process_start_mark(pid)
    if not isinstance(mark, str):
        return current is not None
    return current == mark


__all__ = [
    "ProcessJournal",
    "ProcessRecord",
    "journal_root",
    "sweep_orphans",
]
