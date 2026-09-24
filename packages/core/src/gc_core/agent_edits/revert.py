"""Откат правок агента к базе представления (Р8).

**Контракт безопасности (жёсткий).** ``baseline_for`` обязан отдавать ТОЛЬКО
точный снимок — ``SnapshotStore.session_baseline`` / ``turn_baseline``. Счётная
база шелл-канала (``counting_*_baseline``, содержимое ИНДЕКСА git) сюда не
подаётся никогда: индекс не содержит несохранённых ручных правок пользователя,
и запись его содержимого поверх файла стёрла бы их молча. Пути, у которых
достоверного «до» нет, приходят сюда с ``FileEntry.revertable=False`` и до
записи не доезжают вовсе.

Откат созданного файла решается по ВИДУ правки (``kind == "created"``), а не по
«базы нет» — это защита в глубину. Снимок у созданного пути физически
существовать может: рекордер снимает ``.orig``, только когда старый контент
был, то есть со ВТОРОЙ правки (тул создал файл, потом дописал строку). Такой
снимок — не «до», а середина набора, и условие через базу однажды подсунуло бы
его в ``atomic_replace``, записав пользователю файл с содержимым, которого у
него никогда не было.

Слой данных: без UI. Причины отказов — машинные коды (``REASON_*``), формулировки
для пользователя собирает экран.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from gc_core.agent_edits.deltas import FileEntry, abs_path_for_key
from gc_core.utils.atomic_io import atomic_replace

# Причины, по которым путь не откачен. Машинные коды: слой данных не знает UI.
REASON_NOT_REVERTABLE = "not_revertable"       # достоверной базы для отката нет
REASON_NOTHING_TO_DELETE = "nothing_to_delete"  # созданный файл уже удалён — noop
REASON_NO_BASELINE = "no_baseline"             # снимка «до» нет (или он пропал)
REASON_WRITE_FAILED = "write_failed"           # запись/удаление не удались
REASON_UNKNOWN_PATH = "unknown_path"           # пути нет в наборе представления

_TMP_SUFFIX = ".revert.tmp"


@dataclass(frozen=True)
class RevertOutcome:
    restored: list[str] = field(default_factory=list)          # содержимое возвращено
    deleted: list[str] = field(default_factory=list)           # созданный файл удалён
    skipped: list[tuple[str, str]] = field(default_factory=list)  # (путь, причина)


def revert_paths(
    path_keys: Sequence[str],
    entries_by_path: Mapping[str, FileEntry],
    baseline_for: Callable[[str], Optional[bytes]],
    launch_root: str,
) -> RevertOutcome:
    """Вернуть файлы к базе представления. Никогда не бросает: неудача — в ``skipped``.

    ``baseline_for`` обязан отдавать ТОЧНЫЙ снимок (см. контракт безопасности в
    докстринге модуля).

    Порядок проверок — часть контракта, переставлять нельзя:

    1. ``entry.revertable is False`` → сразу в ``skipped``: достоверного «до»
       нет (правка командой, «создан и удалён в наборе»);
    2. ``entry.kind == "created"`` → удаление файла, база НЕ спрашивается вовсе;
       файла на диске уже нет — ``skipped`` как noop;
    3. иначе — точная база поверх файла через ``atomic_replace``.

    Отсутствующая база у ``revertable``-пути — штатный ``skipped``, а не
    падение: окно конкурентных сессий может снести снимок между показом списка
    и подтверждением отката.
    """
    outcome = RevertOutcome()
    for key in path_keys:
        entry = entries_by_path.get(key)
        if entry is None:
            outcome.skipped.append((key, REASON_UNKNOWN_PATH))
            continue
        if not entry.revertable:
            outcome.skipped.append((key, REASON_NOT_REVERTABLE))
            continue

        abs_path = abs_path_for_key(launch_root, key)
        if entry.kind == "created":
            _delete_created(key, abs_path, outcome)
            continue
        _restore(key, abs_path, baseline_for, outcome)
    return outcome


def _delete_created(key: str, abs_path: Path, outcome: RevertOutcome) -> None:
    """Откат созданного файла — удаление: до набора этого файла не было."""
    if not abs_path.exists():
        outcome.skipped.append((key, REASON_NOTHING_TO_DELETE))
        return
    try:
        abs_path.unlink()
    except OSError:
        outcome.skipped.append((key, REASON_WRITE_FAILED))
        return
    outcome.deleted.append(key)


def _restore(
    key: str,
    abs_path: Path,
    baseline_for: Callable[[str], Optional[bytes]],
    outcome: RevertOutcome,
) -> None:
    try:
        baseline = baseline_for(key)
    except OSError:
        baseline = None      # снимок унесли из-под нас — это не повод падать
    if baseline is None:
        outcome.skipped.append((key, REASON_NO_BASELINE))
        return

    tmp = abs_path.with_name(abs_path.name + _TMP_SUFFIX)
    try:
        abs_path.parent.mkdir(parents=True, exist_ok=True)   # файл мог быть удалён
        tmp.write_bytes(baseline)
        _carry_mode(abs_path, tmp)
        atomic_replace(tmp, abs_path)
    except OSError:
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        outcome.skipped.append((key, REASON_WRITE_FAILED))
        return
    outcome.restored.append(key)


def _carry_mode(source: Path, dest: Path) -> None:
    """Перенести права ``source`` на ``dest`` — до замены, пока это временный файл.

    Восстановленный файл пишется заново и без этого получает режим по umask:
    откат ``scripts/deploy.sh`` вернул бы верное содержимое без бита
    исполнения — без ошибки, ломается позже при запуске.

    Отказ здесь штатный и молчаливый: у отката удаления исходного файла нет
    вовсе (переносить нечего), а на Windows биты доступа ничего не решают.
    Содержимое важнее режима — из-за прав откат не отменяется.
    """
    try:
        os.chmod(dest, stat.S_IMODE(source.stat().st_mode))
    except OSError:
        pass
