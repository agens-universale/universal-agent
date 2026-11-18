"""FileTaskStore.get_record — запись треда не перечитывается, пока его
файлы те же (опрос хвоста спрашивает её на каждом тике)."""

import json
import os
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore

_TID = "t1"


def _store(tmp_path) -> FileTaskStore:
    (Path(tmp_path) / f"meta_cli_{_TID}.json").write_text(
        json.dumps(
            {
                "thread_id": _TID,
                "updated_at": "2026-05-21T12:00:00+00:00",
                "messages_count": 1,
                "tui": {"skills": ["a"]},
            }
        ),
        encoding="utf-8",
    )
    return FileTaskStore(threads_dir=str(tmp_path), system="cli")


def _count_reads(store: FileTaskStore, monkeypatch) -> list:
    reads: list = []
    get = store._threads.get

    async def counted(key):
        reads.append(key)
        return await get(key)

    monkeypatch.setattr(store._threads, "get", counted)
    return reads


def test_same_files_no_reread(tmp_path, monkeypatch):
    store = _store(tmp_path)
    reads = _count_reads(store, monkeypatch)
    first = store.get_record(_TID)
    second = store.get_record(_TID)
    assert len(reads) == 1
    assert first == second


def test_meta_change_rereads(tmp_path, monkeypatch):
    store = _store(tmp_path)
    reads = _count_reads(store, monkeypatch)
    store.get_record(_TID)
    store.update_tui_meta(_TID, {"skills": ["a", "b"]})
    record = store.get_record(_TID)
    assert len(reads) == 2
    assert record.frontend["tui"]["skills"] == ["a", "b"]


def test_outside_write_same_size_rereads(tmp_path, monkeypatch):
    """Meta переписана мимо хранилища тем же размером — новое время
    записи, запись читается заново."""
    store = _store(tmp_path)
    reads = _count_reads(store, monkeypatch)
    store.get_record(_TID)
    meta = Path(tmp_path) / f"meta_cli_{_TID}.json"
    text = meta.read_text(encoding="utf-8").replace('"a"', '"z"')
    stat = meta.stat()
    meta.write_text(text, encoding="utf-8")
    os.utime(meta, ns=(stat.st_atime_ns, stat.st_mtime_ns + 1_000_000))
    record = store.get_record(_TID)
    assert len(reads) == 2
    assert record.frontend["tui"]["skills"] == ["z"]


def test_cached_record_is_a_copy(tmp_path):
    """Вложенные словари записи изменяемы: правка отданной записи не
    попадает в кеш."""
    store = _store(tmp_path)
    store.get_record(_TID).frontend["tui"]["skills"].append("x")
    assert store.get_record(_TID).frontend["tui"]["skills"] == ["a"]


def test_missing_thread_not_cached(tmp_path, monkeypatch):
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    reads = _count_reads(store, monkeypatch)
    assert store.get_record("nope") is None
    assert store.get_record("nope") is None
    assert len(reads) == 2
