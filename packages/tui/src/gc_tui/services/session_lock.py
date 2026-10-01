"""Exclusive TUI attach lock для thread_id: lease-файл + heartbeat + TTL.

Канон: docs_md/tui/session-lock/SPEC.md. Meta/runtime не участвуют.
"""

from __future__ import annotations

import json
import os
import time
from datetime import UTC, datetime
from functools import wraps
from pathlib import Path
from typing import Any, Optional

from gc_core.thread_store import sanitize_system_id, validate_thread_id
from gc_core.thread_store_journal import (
    thread_file_read_scope,
    thread_read_scope,
)
from gc_core.utils.atomic_io import atomic_replace
from gc_core.utils.user_text import read_user_text_or_none

from gc_tui.services.relative_time import parse_iso_utc

TTL_SECONDS = 10.0
HEARTBEAT_MIN_INTERVAL_S = 1.0
BUSY_MESSAGE = "сессия занята"


def lock_path(threads_dir: str, system: str, thread_id: str) -> str:
    sys_norm = sanitize_system_id(system)
    tid = validate_thread_id(thread_id)
    return os.path.join(threads_dir, "locks", f"lock_{sys_norm}_{tid}.json")


def _utc_now() -> datetime:
    return datetime.now(UTC)


def _now_iso(now: Optional[datetime] = None) -> str:
    return (now or _utc_now()).astimezone(UTC).isoformat()


def _read_lock(path: str) -> Optional[dict[str, Any]]:
    """Прочитать lock. None = отсутствует / битый / пустой JSON (как stale)."""
    raw = read_user_text_or_none(path)
    if raw is None:
        return None
    if not raw.strip():
        return None
    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict):
        return None
    tui_id = data.get("tui_id")
    heartbeat_at = data.get("heartbeat_at")
    if not isinstance(tui_id, str) or not tui_id.strip():
        return None
    if (
        not isinstance(heartbeat_at, str)
        or parse_iso_utc(heartbeat_at) is None
    ):
        return None
    return {"tui_id": tui_id.strip(), "heartbeat_at": heartbeat_at}


def read_lock(path: str) -> Optional[dict[str, Any]]:
    """Read a lease after recovery under its physical thread-file lock."""
    target = Path(path)
    if (
        target.parent.name == "locks"
        and target.name.startswith("lock_")
        and target.suffix == ".json"
    ):
        stem = target.name.removeprefix("lock_").removesuffix(".json")
        with thread_file_read_scope(str(target.parent.parent), stem):
            return _read_lock(path)
    return _read_lock(path)


def is_fresh(
    lock: dict[str, Any],
    *,
    now: Optional[datetime] = None,
    ttl: float = TTL_SECONDS,
) -> bool:
    """True если heartbeat свежий (now - heartbeat_at ≤ TTL)."""
    hb = parse_iso_utc(lock.get("heartbeat_at"))
    if hb is None:
        return False
    current = now or _utc_now()
    if current.tzinfo is None:
        current = current.replace(tzinfo=UTC)
    return (current - hb).total_seconds() <= ttl


def is_held_by_other(
    threads_dir: str,
    system: str,
    thread_id: str,
    tui_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Чужой свежий lock → занято (для UX /sessions и deny до выбора)."""
    path = lock_path(threads_dir, system, thread_id)
    lock = _read_lock(path)
    if lock is None:
        return False
    if lock["tui_id"] == tui_id:
        return False
    return is_fresh(lock, now=now)


def _write_lock(
    path: str, tui_id: str, *, now: Optional[datetime] = None
) -> None:
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    payload = {"tui_id": tui_id, "heartbeat_at": _now_iso(now)}
    tmp_path = f"{path}.tmp"
    with open(tmp_path, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    atomic_replace(tmp_path, path)


def try_acquire(
    threads_dir: str,
    system: str,
    thread_id: str,
    tui_id: str,
    *,
    now: Optional[datetime] = None,
) -> bool:
    """Попытаться стать holder: write → re-read. False при свежем чужом или lost race."""
    path = lock_path(threads_dir, system, thread_id)
    existing = _read_lock(path)
    if (
        existing is not None
        and existing["tui_id"] != tui_id
        and is_fresh(existing, now=now)
    ):
        return False
    _write_lock(path, tui_id, now=now)
    after = _read_lock(path)
    return after is not None and after["tui_id"] == tui_id


def heartbeat(
    threads_dir: str,
    system: str,
    thread_id: str,
    tui_id: str,
    *,
    last_mono: float = 0.0,
    mono: Optional[float] = None,
    now: Optional[datetime] = None,
) -> float:
    """Обновить heartbeat_at, если мы holder. Throttle ≤ 1 Гц. Возвращает новый last_mono."""
    current_mono = time.monotonic() if mono is None else mono
    if current_mono - last_mono < HEARTBEAT_MIN_INTERVAL_S:
        return last_mono
    path = lock_path(threads_dir, system, thread_id)
    existing = _read_lock(path)
    if existing is None or existing["tui_id"] != tui_id:
        return last_mono
    _write_lock(path, tui_id, now=now)
    return current_mono


def release(
    threads_dir: str,
    system: str,
    thread_id: str,
    tui_id: Optional[str] = None,
) -> None:
    """Best-effort удалить lock. При tui_id — только свой."""
    path = lock_path(threads_dir, system, thread_id)
    if tui_id is not None:
        existing = _read_lock(path)
        if existing is not None and existing["tui_id"] != tui_id:
            return
    try:
        os.remove(path)
    except OSError:
        pass


def _with_thread_scope(function):
    @wraps(function)
    def guarded(threads_dir, system, thread_id, *args, **kwargs):
        with thread_read_scope(threads_dir, system, thread_id):
            return function(threads_dir, system, thread_id, *args, **kwargs)

    return guarded


# A reset and a heartbeat cannot interleave between holder check and write.
# Private reads inside these operations already hold the exact thread scope.
is_held_by_other = _with_thread_scope(is_held_by_other)
try_acquire = _with_thread_scope(try_acquire)
heartbeat = _with_thread_scope(heartbeat)
release = _with_thread_scope(release)
