"""Чтение истории для модели ленты — сегменты и курсор одним снимком
(спека шага 2 окна ленты, §5.1 «Чтение и входы»)."""

from __future__ import annotations

import pytest
from feed_dump import HISTORY_THREAD, load_history_fixture, write_history
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_tui.services.stores.file_store import FileTaskStore

_FIXTURES = ["calls", "compaction", "model_change", "withdrawn"]
_KEY = ThreadKey(system="cli", thread=HISTORY_THREAD)
_READS = ("get_thread", "read_stream", "read_archive")


def _store(tmp_path, name: str) -> FileTaskStore:
    write_history(str(tmp_path), load_history_fixture(name))
    return FileTaskStore(str(tmp_path))


def _ids(records: list[dict]) -> list[str]:
    return [
        rec["payload"]["id"]
        for rec in records
        if isinstance(rec.get("payload"), dict)
    ]


@pytest.mark.parametrize("name", _FIXTURES)
def test_read_history_segments_and_cursor(tmp_path, name):
    """Сегменты — как у ``read_segments``, курсор — сразу за ними: хвост
    за ним пуст и не сбрасывает показ."""
    store = _store(tmp_path, name)
    segments, cursor = store.read_history(HISTORY_THREAD)
    assert segments == store.read_segments(HISTORY_THREAD)
    tail = store.tail_events(HISTORY_THREAD, cursor)
    assert tail.new_records == []
    assert not tail.stream_reset
    assert tail.compaction is None


def test_read_history_of_missing_thread(tmp_path):
    segments, cursor = FileTaskStore(str(tmp_path)).read_history("none")
    assert segments == [{"index": 0, "messages": [], "compacted_by": None}]
    assert cursor.position_token is None


@pytest.mark.parametrize("name", ["calls", "compaction"])
@pytest.mark.parametrize("at", range(8))
def test_read_history_single_snapshot(tmp_path, monkeypatch, name, at):
    """Дозапись посреди чтения (на ``at``-м обращении к хранилищу): каждая
    запись треда — ровно в одном из «сегменты» и «хвост за курсором»."""
    store = _store(tmp_path, name)
    writer = Threads(FileThreadStore(str(tmp_path)))
    extra = {"id": "m_extra", "role": "user", "content": "дозапись"}
    calls = [0]
    inner = store._threads.store

    def counted(method):
        async def wrapper(*args, **kwargs):
            if calls[0] == at:
                await writer.append_messages(_KEY, [extra])
            calls[0] += 1
            return await method(*args, **kwargs)

        return wrapper

    for name_ in _READS:
        monkeypatch.setattr(inner, name_, counted(getattr(inner, name_)))
    segments, cursor = store.read_history(HISTORY_THREAD)
    monkeypatch.undo()
    if calls[0] <= at:
        store.append_messages(HISTORY_THREAD, [extra])
    tail = store.tail_events(HISTORY_THREAD, cursor)
    shown = [i for seg in segments for i in _ids(seg["messages"])]
    full = store.read_segments(HISTORY_THREAD)
    assert shown + _ids(tail.new_records) == [
        i for seg in full for i in _ids(seg["messages"])
    ]
    assert not tail.stream_reset


def test_subagent_trace_reread_only_when_file_changes(tmp_path, monkeypatch):
    """Трасса субагентов не перечитывается, пока файл тот же (размер и
    время записи): окно ленты спрашивает её на каждый кусок."""
    import os

    from gc_core.threads.file_layout import subagent_trace_path

    store = _store(tmp_path, "subagents")
    streams: list[str] = []
    real = FileThreadStore.read_stream

    async def counted(self, key, stream, **kwargs):
        streams.append(stream)
        return await real(self, key, stream, **kwargs)

    monkeypatch.setattr(FileThreadStore, "read_stream", counted)
    first = store.read_subagent_trace(HISTORY_THREAD)
    assert first
    assert store.read_subagent_trace(HISTORY_THREAD) == first
    assert streams == ["subagent_events"]
    path = subagent_trace_path(str(tmp_path), "cli", HISTORY_THREAD)
    stat = os.stat(path)
    os.utime(path, ns=(stat.st_atime_ns, stat.st_mtime_ns + 10**9))
    assert store.read_subagent_trace(HISTORY_THREAD) == first
    assert streams == ["subagent_events"] * 2


def test_subagent_trace_cache_with_relative_dir_after_cwd_change(
    tmp_path, monkeypatch
):
    """Отпечаток трассы — по каталогу, как его читает хранилище
    (абсолютный): относительный каталог и смена cwd кеш не отключают."""
    threads = tmp_path / "threads"
    write_history(str(threads), load_history_fixture("subagents"))
    monkeypatch.chdir(tmp_path)
    store = FileTaskStore("threads")
    monkeypatch.chdir(threads)
    streams: list[str] = []
    real = FileThreadStore.read_stream

    async def counted(self, key, stream, **kwargs):
        streams.append(stream)
        return await real(self, key, stream, **kwargs)

    monkeypatch.setattr(FileThreadStore, "read_stream", counted)
    first = store.read_subagent_trace(HISTORY_THREAD)
    assert first
    assert store.read_subagent_trace(HISTORY_THREAD) == first
    assert streams == ["subagent_events"]
