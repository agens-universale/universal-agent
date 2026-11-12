"""Раскладка файлов треда develop — один код для старого и нового хранилища.

Имена файлов, формат строк истории и трассы, чтение и штампы meta. Старый
``gc_core.thread_store`` и ``FileThreadStore`` v2 пользуются этим модулем
оба, поэтому формат на диске один и тот же по построению.

Только стандартная библиотека и лёгкие модули ядра: модуль импортируют и
хранилище, и ``thread_store``, который тянет states и тулы.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any

from gc_core.execution.message_keys import make_message_key
from gc_core.persistence.schema import (
    STATE_SCHEMA_VERSION,
    reject_legacy_memory_keys,
)
from gc_core.thread_paths import sanitize_system_id, validate_thread_id

META_SCHEMA_VERSION = 2
ARCHIVE_MARK = "__pre_summary__"
IMAGES_DIR = "images"


def _stem(system_id: str, thread_id: str) -> str:
    return f"{sanitize_system_id(system_id)}_{validate_thread_id(thread_id)}"


def thread_paths(
    threads_dir: str, system_id: str, thread_id: str
) -> tuple[str, str]:
    """История и meta: ``data_<system>_<thread>.jsonl``, ``meta_….json``."""
    stem = _stem(system_id, thread_id)
    return (
        os.path.join(threads_dir, f"data_{stem}.jsonl"),
        os.path.join(threads_dir, f"meta_{stem}.json"),
    )


def legacy_thread_paths(threads_dir: str, thread_id: str) -> tuple[str, str]:
    """Старый формат без system в имени."""
    normalized = validate_thread_id(thread_id)
    return (
        os.path.join(threads_dir, f"data_{normalized}.jsonl"),
        os.path.join(threads_dir, f"meta_{normalized}.json"),
    )


def subagent_trace_path(
    threads_dir: str, system_id: str, thread_id: str
) -> str:
    """Трасса субагентов: ``data_subagents_<system>_<thread>.jsonl``."""
    stem = _stem(system_id, thread_id)
    return os.path.join(threads_dir, f"data_subagents_{stem}.jsonl")


def plan_file_path(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Файл плана сессии для человека: plans/plan_{system}_{thread_id}.md.

    Лежит под каталогом сессий, а не в отдельной папке проекта: удаление
    треда знает только этот каталог, и иначе файл осиротел бы.
    """
    return os.path.join(
        threads_dir, "plans", f"plan_{_stem(system_id, thread_id)}.md"
    )


def edits_journal_path(
    threads_dir: str, system_id: str, thread_id: str
) -> Path:
    """Журнал правок агента: ``edits_<system>_<thread>.jsonl``."""
    return Path(threads_dir) / f"edits_{_stem(system_id, thread_id)}.jsonl"


def stream_path(
    threads_dir: str, system_id: str, thread_id: str, stream: str
) -> str:
    """Поток v2 вне формата develop (``inbox``, потоки фронтендов)."""
    stem = _stem(system_id, thread_id)
    return os.path.join(threads_dir, f"stream_{stream}_{stem}.jsonl")


def archive_paths(
    threads_dir: str, system_id: str, thread_id: str, suffix: str
) -> tuple[str, str]:
    """Архив истории перед сжатием: ``data_…__pre_summary__<suffix>.jsonl``."""
    stem = _stem(system_id, thread_id)
    return (
        os.path.join(threads_dir, f"data_{stem}{ARCHIVE_MARK}{suffix}.jsonl"),
        os.path.join(threads_dir, f"meta_{stem}{ARCHIVE_MARK}{suffix}.json"),
    )


def archive_suffixes(
    threads_dir: str, system_id: str, thread_id: str
) -> list[str]:
    """Суффиксы архивов треда в хронологическом порядке."""
    prefix = f"data_{_stem(system_id, thread_id)}{ARCHIVE_MARK}"
    if not os.path.isdir(threads_dir):
        return []
    return sorted(
        name[len(prefix) : -len(".jsonl")]
        for name in os.listdir(threads_dir)
        if name.startswith(prefix) and name.endswith(".jsonl")
    )


# --- история ------------------------------------------------------------------


def parse_message_record(row: str) -> dict[str, Any] | None:
    """Запись типа message из строки jsonl (в т.ч. в строковой обёртке)."""
    try:
        record = json.loads(row)
    except json.JSONDecodeError:
        return None
    # Допускаем строковую обёртку (двойной json.dumps строки записи).
    if isinstance(record, str):
        try:
            record = json.loads(record)
        except json.JSONDecodeError:
            return None
    if not isinstance(record, dict) or record.get("type") != "message":
        return None
    return record


def history_line(
    system_id: str, thread_id: str, seq: int, ts: str, payload: dict
) -> str:
    """Строка истории develop, с переводом строки."""
    record = {
        "type": "message",
        "thread_id": thread_id,
        "system": sanitize_system_id(system_id),
        "seq": seq,
        "ts": ts,
        "payload": payload,
    }
    return json.dumps(record, ensure_ascii=False) + "\n"


def _legacy_identity(system_id: str, thread_id: str, row: str) -> str:
    # Записи без id получают один и тот же id на каждом чтении, в том числе
    # после перезапуска процесса и удаления соседних записей.
    identity = f"{sanitize_system_id(system_id)}:{thread_id}:{row}"
    return "m_" + hashlib.sha256(identity.encode()).hexdigest()[:32]


def _with_identity(
    payload: dict, keys: set[str], system_id: str, thread_id: str, row: str
) -> dict:
    """Сообщение с id: у старой записи без id он синтезируется из строки."""
    # Ключ — по записи как есть, до синтеза id: так же он считается при
    # записи, и сверка после конфликта ревизии сходится.
    keys.add(make_message_key(payload))
    if isinstance(payload.get("id"), str) and payload["id"]:
        return payload
    payload = dict(payload)
    payload["id"] = _legacy_identity(system_id, thread_id, row)
    # И ключ с синтезированным id: эта запись в памяти уже с ним, и история,
    # отправленная целиком, не должна дописаться дважды.
    keys.add(make_message_key(payload))
    return payload


def read_history_lines(
    lines, system_id: str, thread_id: str
) -> tuple[list[tuple[int, str, dict]], set[str], int]:
    """Записи истории ``(seq, ts, payload)``, ключи сообщений и max seq."""
    records: list[tuple[int, str, dict]] = []
    keys: set[str] = set()
    max_seq = 0
    for line in lines:
        row = line.strip()
        record = parse_message_record(row) if row else None
        if record is None:
            continue
        seq = record.get("seq")
        if isinstance(seq, int) and seq > max_seq:
            max_seq = seq
        payload = record.get("payload")
        if not isinstance(payload, dict):
            continue
        payload = _with_identity(payload, keys, system_id, thread_id, row)
        records.append(
            (
                seq if isinstance(seq, int) else 0,
                str(record.get("ts") or ""),
                payload,
            )
        )
    return records, keys, max_seq


def read_history_file(
    path: str, system_id: str, thread_id: str
) -> tuple[list[tuple[int, str, dict]], set[str], int]:
    """То же, что :func:`read_history_lines`, по файлу; нет файла — пусто."""
    if not os.path.exists(path):
        return [], set(), 0
    with open(path, encoding="utf-8") as stream:
        return read_history_lines(stream, system_id, thread_id)


def history_file(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Файл истории треда: канонический, иначе старый без system в имени."""
    data_path, _ = thread_paths(threads_dir, system_id, thread_id)
    if os.path.exists(data_path):
        return data_path
    legacy, _ = legacy_thread_paths(threads_dir, thread_id)
    return legacy if os.path.exists(legacy) else data_path


# --- meta ---------------------------------------------------------------------


def meta_file(threads_dir: str, system_id: str, thread_id: str) -> str:
    """Файл meta треда: канонический, иначе старый без system в имени."""
    _, meta_path = thread_paths(threads_dir, system_id, thread_id)
    if os.path.exists(meta_path):
        return meta_path
    _, legacy = legacy_thread_paths(threads_dir, thread_id)
    return legacy if os.path.exists(legacy) else meta_path


def read_meta_file(path: str) -> dict[str, Any]:
    if not os.path.exists(path):
        return {}
    with open(path, encoding="utf-8") as stream:
        data = json.load(stream)
    return data if isinstance(data, dict) else {}


def stamp_meta(
    meta: dict[str, Any], system_id: str, thread_id: str, now: str
) -> dict[str, Any]:
    """Штампы meta при записи — как всегда делал ``save_thread_meta``."""
    stamped = dict(meta)
    stamped["state_schema_version"] = STATE_SCHEMA_VERSION
    reject_legacy_memory_keys(stamped)
    stamped["thread_id"] = validate_thread_id(thread_id)
    stamped["system"] = sanitize_system_id(system_id)
    stamped["schema_version"] = META_SCHEMA_VERSION
    stamped["updated_at"] = now
    if not stamped.get("created_at"):
        stamped["created_at"] = now
    return stamped


def meta_text(meta: dict[str, Any]) -> str:
    """Meta на диске: ``indent=2``, без перевода строки в конце."""
    return json.dumps(meta, ensure_ascii=False, indent=2)
