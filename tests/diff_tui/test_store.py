"""DiffStore: snapshot/approve/undo, LRU, stale cleanup (spec2 T1–T4, T6–T9)."""
from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import patch

import pytest

from config_modules import env_vars
from diff_tui.paths import orig_key
from diff_tui.store import DiffStore


@pytest.fixture
def proj(tmp_path, monkeypatch):
    root = tmp_path / "proj"
    root.mkdir()
    (root / "src").mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    monkeypatch.chdir(root)
    return root


def _store(proj: Path, **kwargs) -> DiffStore:
    return DiffStore(str(proj), **kwargs)


def _orig_path(store: DiffStore, key: str) -> Path:
    return store.diffs_dir / f"{orig_key(key)}.orig"


# --- T1: первая мутация существующего файла → .orig + M ---


def test_t1_first_mutation_creates_orig_and_modified(proj):
    target = proj / "src" / "foo.py"
    target.write_text("baseline\n", encoding="utf-8")
    store = _store(proj)

    key = store.register_mutation(str(target))
    assert key == "src/foo.py"
    assert _orig_path(store, key).read_text(encoding="utf-8") == "baseline\n"
    entry = store.load_index()[key]
    assert entry["status"] == "modified"
    assert entry["baseline_exists"] is True


def test_t1_new_file_registers_created_without_orig(proj):
    target = proj / "src" / "new.py"
    assert not target.exists()
    store = _store(proj)

    key = store.register_mutation(str(target))
    assert key == "src/new.py"
    assert not _orig_path(store, key).exists()
    entry = store.load_index()[key]
    assert entry["status"] == "created"
    assert entry["baseline_exists"] is False


# --- T2: вторая мутация → .orig не перезаписан ---


def test_t2_second_mutation_does_not_overwrite_orig(proj):
    target = proj / "src" / "foo.py"
    target.write_text("v1\n", encoding="utf-8")
    store = _store(proj)
    store.register_mutation(str(target))

    target.write_text("v2\n", encoding="utf-8")
    store.register_mutation(str(target))

    key = "src/foo.py"
    assert _orig_path(store, key).read_text(encoding="utf-8") == "v1\n"
    assert store.load_index()[key]["status"] == "modified"


# --- T3: approve → .orig = disk; следующая мутация не перезаписывает ---


def test_t3_approve_rewrites_orig_then_mutation_keeps_it(proj):
    target = proj / "src" / "foo.py"
    target.write_text("old\n", encoding="utf-8")
    store = _store(proj)
    store.register_mutation(str(target))

    target.write_text("approved\n", encoding="utf-8")
    assert store.approve(str(target)) is True
    key = "src/foo.py"
    assert _orig_path(store, key).read_text(encoding="utf-8") == "approved\n"
    entry = store.load_index()[key]
    assert entry["status"] == "modified"
    assert entry["baseline_exists"] is True

    target.write_text("pending-again\n", encoding="utf-8")
    store.register_mutation(str(target))
    assert _orig_path(store, key).read_text(encoding="utf-8") == "approved\n"


def test_t3_approve_created_promotes_to_modified(proj):
    target = proj / "src" / "new.py"
    store = _store(proj)
    store.register_mutation(str(target))  # created, no file yet
    target.write_text("brand-new\n", encoding="utf-8")

    assert store.approve(str(target)) is True
    key = "src/new.py"
    assert _orig_path(store, key).read_text(encoding="utf-8") == "brand-new\n"
    entry = store.load_index()[key]
    assert entry["status"] == "modified"
    assert entry["baseline_exists"] is True


# --- T4: undo M → disk == .orig ---


def test_t4_undo_modified_restores_disk(proj):
    target = proj / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    store = _store(proj)
    store.register_mutation(str(target))
    target.write_text("changed\n", encoding="utf-8")

    result = store.undo(str(target))
    assert result.ok is True
    assert target.read_text(encoding="utf-8") == "base\n"


def test_t4b_undo_created_deletes_file(proj):
    target = proj / "src" / "new.py"
    store = _store(proj)
    store.register_mutation(str(target))
    target.write_text("brand new\n", encoding="utf-8")

    result = store.undo(str(target))
    assert result.ok is True
    assert not target.exists()
    assert "src/new.py" not in store.load_index()


# --- T6: вне perimeter → не в index ---


def test_t6_outside_perimeter_not_registered(proj, tmp_path):
    outside = tmp_path / "elsewhere" / "x.py"
    outside.parent.mkdir()
    outside.write_text("x\n", encoding="utf-8")
    store = _store(proj)

    assert store.register_mutation(str(outside)) is None
    assert store.load_index() == {}


# --- T7: binary → не трекается ---


def test_t7_binary_not_tracked(proj):
    target = proj / "src" / "blob.bin"
    target.write_bytes(b"\x00\x01\x02\xffbinary")
    store = _store(proj)

    assert store.register_mutation(str(target)) is None
    assert store.load_index() == {}
    assert list(store.diffs_dir.glob("*.orig")) == []


# --- T8: атомарность approve — при сбое index не «approved» без .orig ---


def test_t8_approve_failure_after_orig_keeps_index_consistent(proj):
    """Порядок: .orig → index. Если index write падает — старый index остаётся."""
    target = proj / "src" / "foo.py"
    target.write_text("old\n", encoding="utf-8")
    store = _store(proj)
    store.register_mutation(str(target))
    key = "src/foo.py"
    before = store.load_index()

    target.write_text("new\n", encoding="utf-8")
    with patch.object(DiffStore, "save_index", side_effect=OSError("boom")):
        with pytest.raises(OSError, match="boom"):
            store.approve(str(target))

    # .orig уже мог обновиться (порядок .orig → index), но index не «approved»-лживый:
    # запись в index не применена — статус/набор как до approve.
    assert store.load_index() == before
    # .orig на диске = new (шаг 2 прошёл); index ещё указывает на pre-approve состояние.
    # Инвариант спеки: не оставлять index в состоянии approved без актуального .orig.
    # Здесь index НЕ approved (остался старый) — согласованность сохранена.
    assert _orig_path(store, key).read_text(encoding="utf-8") == "new\n"


# --- T9: LRU eviction + stale cleanup ---


def test_t9_lru_evicts_oldest_and_index_entry(proj):
    store = _store(proj, cache_max_orig=100)  # без eviction на setup
    for i, name in enumerate(("a.py", "b.py", "c.py")):
        p = proj / "src" / name
        p.write_text(f"{name}-base\n", encoding="utf-8")
        store.register_mutation(str(p))
        p.write_text(f"{name}-dirty\n", encoding="utf-8")  # dirty → не clean-evict
        # Развести mtime .orig (oldest-first).
        orig = _orig_path(store, f"src/{name}")
        os.utime(orig, (1_000_000 + i, 1_000_000 + i))

    store.cache_max_orig = 2
    store._maybe_evict(store.load_index())

    origs = list(store.diffs_dir.glob("*.orig"))
    assert len(origs) == 2
    index = store.load_index()
    assert len(index) == 2
    assert "src/a.py" not in index
    assert "src/b.py" in index
    assert "src/c.py" in index


def test_t9_stale_index_cleanup_on_list(proj):
    store = _store(proj)
    target = proj / "src" / "foo.py"
    target.write_text("base\n", encoding="utf-8")
    store.register_mutation(str(target))
    key = "src/foo.py"
    _orig_path(store, key).unlink()

    entries = store.list_entries(cleanup_stale=True)
    assert key not in {e.key for e in entries}
    assert key not in store.load_index()


def test_t9_created_without_orig_not_treated_as_stale(proj):
    store = _store(proj)
    target = proj / "src" / "new.py"
    store.register_mutation(str(target))
    entries = store.list_entries(cleanup_stale=True)
    assert any(e.key == "src/new.py" and e.status == "created" for e in entries)
    assert "src/new.py" in store.load_index()
