import os
import time

import pytest

from gc_computer_mcp.core.state_store import (
    StateExpiredError,
    StateNotFoundError,
    StateStore,
)


@pytest.fixture
def tmp_temp(monkeypatch, tmp_path):
    monkeypatch.setenv("TEMP", str(tmp_path))
    monkeypatch.setenv("TMP", str(tmp_path))
    return tmp_path


def test_path_uses_fixed_default_session_id(tmp_temp):
    """Default session_id is a fixed 'default' string so it's stable across
    multiple short-lived MCP processes spawned from the same agent.
    """
    store = StateStore(session_id=None, ttl_seconds=3600)
    expected = tmp_temp / "gc-computer-mcp" / "last_state_default.json"
    assert store.path == expected


def test_two_store_instances_share_default_path(tmp_temp):
    """Two independently-constructed StateStores (simulating two MCP calls)
    with no explicit session_id must land on the same file.
    """
    a = StateStore(session_id=None, ttl_seconds=3600)
    b = StateStore(session_id=None, ttl_seconds=3600)
    assert a.path == b.path


def test_path_uses_session_id_override(tmp_temp):
    store = StateStore(session_id="abc-123", ttl_seconds=3600)
    expected = tmp_temp / "gc-computer-mcp" / "last_state_abc-123.json"
    assert store.path == expected


def test_save_then_load_roundtrip(tmp_temp):
    store = StateStore(session_id="s1", ttl_seconds=3600)
    snapshot = {"elements": [{"id": 1, "runtime_id": "42/1", "name": "Btn"}]}
    store.save(snapshot)
    loaded = store.load()
    assert loaded["elements"] == snapshot["elements"]
    assert "saved_at" in loaded


def test_load_raises_when_missing(tmp_temp):
    store = StateStore(session_id="s2", ttl_seconds=3600)
    with pytest.raises(StateNotFoundError):
        store.load()


def test_load_raises_when_expired(tmp_temp):
    store = StateStore(session_id="s3", ttl_seconds=1)
    store.save({"elements": []})
    # Manually set mtime into the past
    path = store.path
    past = time.time() - 10
    os.utime(path, (past, past))
    with pytest.raises(StateExpiredError):
        store.load()


def test_atomic_write_no_partial_file(tmp_temp, monkeypatch):
    """If write fails midway, target file must not be corrupted."""
    store = StateStore(session_id="s4", ttl_seconds=3600)
    store.save({"elements": [{"id": 1}]})
    orig_replace = os.replace
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated")
        orig_replace(src, dst)

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError):
        store.save({"elements": [{"id": 999}]})
    # Original still intact:
    loaded = store.load()
    assert loaded["elements"] == [{"id": 1}]


def test_cleanup_removes_old_files(tmp_temp):
    root = tmp_temp / "gc-computer-mcp"
    root.mkdir(parents=True, exist_ok=True)
    old = root / "last_state_9999.json"
    old.write_text("{}")
    past = time.time() - 7200
    os.utime(old, (past, past))
    fresh = root / "last_state_1111.json"
    fresh.write_text("{}")

    from gc_computer_mcp.core.state_store import cleanup_stale

    cleanup_stale(ttl_seconds=3600)
    assert not old.exists()
    assert fresh.exists()


def test_load_treats_corrupt_file_as_missing(tmp_temp):
    store = StateStore(session_id="corrupt", ttl_seconds=3600)
    store.path.parent.mkdir(parents=True, exist_ok=True)
    store.path.write_text("not json at all")
    with pytest.raises(StateNotFoundError):
        store.load()


def test_concurrent_saves_use_different_tmp_files(tmp_temp, monkeypatch):
    """Two processes (simulated by monkeypatching os.getpid) must not share .tmp."""
    store = StateStore(session_id="shared", ttl_seconds=3600)

    monkeypatch.setattr(os, "getpid", lambda: 1111)
    # Intercept tmp path that save would compute
    tmp1 = store.path.with_suffix(f".json.{os.getpid()}.tmp")

    monkeypatch.setattr(os, "getpid", lambda: 2222)
    tmp2 = store.path.with_suffix(f".json.{os.getpid()}.tmp")

    assert tmp1 != tmp2


def test_resolve_default_session_id_returns_fixed_string():
    """The default resolver returns a constant string so it is stable across
    short-lived MCP processes (previous PPID-based default and psutil-walk
    default both failed on Windows due to launcher.exe spawning transient
    python.exe children).
    """
    from gc_computer_mcp.core.state_store import _resolve_default_session_id

    assert _resolve_default_session_id() == "default"
