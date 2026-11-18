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


def _rewrite_in_same_tick(path: Path, text: str) -> None:
    """Файл переписан атомарной заменой, как пишет хранилище, тем же
    размером и в тот же такт времени записи (грубые метки ядра): размер и
    время записи файла и каталога — прежние, нов только inode."""
    stat, folder = path.stat(), path.parent.stat()
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    os.utime(tmp, ns=(stat.st_atime_ns, stat.st_mtime_ns))
    os.replace(tmp, path)
    os.utime(path.parent, ns=(folder.st_atime_ns, folder.st_mtime_ns))


def test_same_size_rewrite_in_same_tick_rereads(tmp_path, monkeypatch):
    store = _store(tmp_path)
    reads = _count_reads(store, monkeypatch)
    store.get_record(_TID)
    meta = Path(tmp_path) / f"meta_cli_{_TID}.json"
    text = meta.read_text(encoding="utf-8").replace('"a"', '"z"')
    _rewrite_in_same_tick(meta, text)
    record = store.get_record(_TID)
    assert len(reads) == 2
    assert record.frontend["tui"]["skills"] == ["z"]


def test_own_write_drops_cached_record(tmp_path, monkeypatch):
    """Своя запись хранилища сбрасывает кеш, даже если отпечаток файлов
    её не заметил: чтение для read-modify-write секции `tui` не должно
    видеть старую запись."""
    store = _store(tmp_path)
    reads = _count_reads(store, monkeypatch)
    monkeypatch.setattr(store._files, "files_stamp", lambda *_a: ("same",))
    store.get_record(_TID)
    store.get_record(_TID)
    store.update_tui_meta(_TID, {"skills": ["b"]})
    record = store.get_record(_TID)
    assert len(reads) == 2
    assert record.frontend["tui"]["skills"] == ["b"]


# ---- пропуск хвоста --------------------------------------------------------


def _msg(i: int) -> dict:
    return {"id": f"m{i}", "role": "user", "content": f"реплика {i}"}


def _history_store(tmp_path, *threads: str) -> FileTaskStore:
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    for tid in threads:
        store.update_tui_meta(tid, {})
        store.append_messages(tid, [_msg(1)])
    return store


def _count_tails(store: FileTaskStore, monkeypatch) -> list:
    tails: list = []
    tail = store._threads.tail

    async def counted(key, *args, **kwargs):
        tails.append(key.thread)
        return await tail(key, *args, **kwargs)

    monkeypatch.setattr(store._threads, "tail", counted)
    return tails


def _ids(result) -> list[str]:
    return [rec["payload"]["id"] for rec in result.new_records]


def test_tail_skipped_until_outside_append(tmp_path, monkeypatch):
    """Хвост без новых записей второй раз не читается; запись мимо
    хранилища (агент) после пропуска — читается."""
    store = _history_store(tmp_path, _TID)
    _segments, cursor = store.read_history(_TID)
    tails = _count_tails(store, monkeypatch)
    first = store.tail_events(_TID, cursor)
    second = store.tail_events(_TID, first.next_cursor)
    assert _ids(first) == _ids(second) == []
    assert tails == [_TID]
    FileTaskStore(str(tmp_path)).append_messages(_TID, [_msg(2)])
    third = store.tail_events(_TID, second.next_cursor)
    assert _ids(third) == ["m2"]
    assert tails == [_TID, _TID]


def test_tail_skip_is_per_thread(tmp_path, monkeypatch):
    """Пропуск помнит тред: хвост другого треда читается."""
    store = _history_store(tmp_path, _TID, "t2")
    _segments, cursor = store.read_history(_TID)
    _segments, other = store.read_history("t2")
    tails = _count_tails(store, monkeypatch)
    store.tail_events(_TID, store.tail_events(_TID, cursor).next_cursor)
    store.tail_events("t2", other)
    assert tails == [_TID, "t2"]
