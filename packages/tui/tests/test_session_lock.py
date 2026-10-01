"""Unit-тесты tui.services.session_lock (SPEC session-lock)."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from gc_tui.services.session_lock import (
    TTL_SECONDS,
    heartbeat,
    is_fresh,
    is_held_by_other,
    lock_path,
    read_lock,
    release,
    try_acquire,
)


def _write_raw(path: Path, payload) -> None:
    path.write_text(json.dumps(payload), encoding="utf-8")


def test_lock_path_canonical(tmp_path):
    p = lock_path(str(tmp_path), "cli", "abc123")
    assert p == str(tmp_path / "locks" / "lock_cli_abc123.json")


def test_acquire_when_absent(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "aaa") is True
    lock = read_lock(lock_path(str(tmp_path), "cli", "t1"))
    assert lock is not None
    assert lock["tui_id"] == "aaa"
    assert lock["heartbeat_at"]


def test_acquire_denies_fresh_other(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "holder") is True
    assert try_acquire(str(tmp_path), "cli", "t1", "other") is False
    assert (
        read_lock(lock_path(str(tmp_path), "cli", "t1"))["tui_id"] == "holder"
    )


def test_acquire_allows_own_tui_id(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True


def test_acquire_stale_after_ttl(tmp_path):
    path = Path(lock_path(str(tmp_path), "cli", "t1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    stale_at = (
        datetime.now(UTC) - timedelta(seconds=TTL_SECONDS + 1)
    ).isoformat()
    _write_raw(path, {"tui_id": "old", "heartbeat_at": stale_at})
    assert try_acquire(str(tmp_path), "cli", "t1", "new") is True
    assert read_lock(str(path))["tui_id"] == "new"


def test_unreadable_lock_treated_as_stale(tmp_path):
    path = Path(lock_path(str(tmp_path), "cli", "t1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("{not-json", encoding="utf-8")
    assert read_lock(str(path)) is None
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True


def test_empty_json_treated_as_stale(tmp_path):
    path = Path(lock_path(str(tmp_path), "cli", "t1"))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("", encoding="utf-8")
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True


def test_acquire_lost_race(tmp_path, monkeypatch):
    from gc_tui.services import session_lock as mod

    real_write = mod._write_lock
    calls = {"n": 0}

    def poisoned(path, tui_id, *, now=None):
        real_write(path, tui_id, now=now)
        if calls["n"] == 0:
            calls["n"] += 1
            real_write(path, "winner", now=now)

    monkeypatch.setattr(mod, "_write_lock", poisoned)
    assert try_acquire(str(tmp_path), "cli", "t1", "loser") is False
    assert (
        read_lock(lock_path(str(tmp_path), "cli", "t1"))["tui_id"] == "winner"
    )


def test_is_fresh_boundary():
    now = datetime(2026, 7, 15, 12, 0, 0, tzinfo=UTC)
    fresh = {
        "tui_id": "x",
        "heartbeat_at": (now - timedelta(seconds=TTL_SECONDS)).isoformat(),
    }
    stale = {
        "tui_id": "x",
        "heartbeat_at": (
            now - timedelta(seconds=TTL_SECONDS + 0.01)
        ).isoformat(),
    }
    assert is_fresh(fresh, now=now) is True
    assert is_fresh(stale, now=now) is False


def test_is_held_by_other(tmp_path):
    assert is_held_by_other(str(tmp_path), "cli", "t1", "me") is False
    assert try_acquire(str(tmp_path), "cli", "t1", "other") is True
    assert is_held_by_other(str(tmp_path), "cli", "t1", "me") is True
    assert is_held_by_other(str(tmp_path), "cli", "t1", "other") is False


def test_heartbeat_throttled(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True
    path = lock_path(str(tmp_path), "cli", "t1")
    first = read_lock(path)["heartbeat_at"]
    last = heartbeat(
        str(tmp_path), "cli", "t1", "me", last_mono=100.0, mono=100.5
    )
    assert last == 100.0
    assert read_lock(path)["heartbeat_at"] == first
    last = heartbeat(
        str(tmp_path), "cli", "t1", "me", last_mono=100.0, mono=101.0
    )
    assert last == 101.0
    assert read_lock(path)["heartbeat_at"] != first


def test_heartbeat_skips_foreign_lock(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "holder") is True
    path = lock_path(str(tmp_path), "cli", "t1")
    before = read_lock(path)["heartbeat_at"]
    heartbeat(str(tmp_path), "cli", "t1", "intruder", last_mono=0.0, mono=10.0)
    assert read_lock(path)["heartbeat_at"] == before


def test_release_deletes_own_lock(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True
    release(str(tmp_path), "cli", "t1", "me")
    assert not Path(lock_path(str(tmp_path), "cli", "t1")).exists()


def test_release_does_not_delete_foreign(tmp_path):
    assert try_acquire(str(tmp_path), "cli", "t1", "holder") is True
    release(str(tmp_path), "cli", "t1", "other")
    assert Path(lock_path(str(tmp_path), "cli", "t1")).exists()


def test_heartbeat_does_not_touch_meta(tmp_path):
    meta = tmp_path / "meta_cli_t1.json"
    meta.write_text('{"thread_id":"t1"}', encoding="utf-8")
    before = meta.read_text(encoding="utf-8")
    assert try_acquire(str(tmp_path), "cli", "t1", "me") is True
    heartbeat(str(tmp_path), "cli", "t1", "me", last_mono=0.0, mono=2.0)
    assert meta.read_text(encoding="utf-8") == before


def test_heartbeat_after_reset_cannot_resurrect_lease(tmp_path):
    from gc_core.thread_store import reset_thread_files

    assert try_acquire(str(tmp_path), "cli", "t1", "holder")
    reset_thread_files(str(tmp_path), "cli", "t1")
    assert heartbeat(str(tmp_path), "cli", "t1", "holder", mono=10) == 0
    assert read_lock(lock_path(str(tmp_path), "cli", "t1")) is None
