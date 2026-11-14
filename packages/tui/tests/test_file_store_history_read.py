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
