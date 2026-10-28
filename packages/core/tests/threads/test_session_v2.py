"""Сессия треда на снимке частей v2: шаг — одна запись без чтения перед
ней, сжатие — одна запись, runtime — запись сбоку; чужие поля (секции
фронтендов, заголовок) переживают ход (``gc_testkit.session_checks``)."""

from __future__ import annotations

from typing import Any

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.meta_map import meta_from_record
from gc_core.threads.models import ThreadKey
from gc_core.threads.store import ThreadStore
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t")


class SpyStore(ThreadStore):
    """Хранилище-соглядатай: имена вызванных операций по порядку."""

    def __init__(self, inner: ThreadStore) -> None:
        self.inner = inner
        self.calls: list[str] = []
        self.threads_dir = getattr(inner, "threads_dir", "")

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def _spy(name):  # noqa: N805  # фабрика методов класса
        async def method(self, *args, **kwargs):
            self.calls.append(name)
            return await getattr(self.inner, name)(*args, **kwargs)

        method.__name__ = name
        return method

    get_thread = _spy("get_thread")
    list_threads = _spy("list_threads")
    read_stream = _spy("read_stream")
    read_archive = _spy("read_archive")
    resolve_alias = _spy("resolve_alias")
    commit = _spy("commit")
    append_side = _spy("append_side")
    claim = _spy("claim")
    release = _spy("release")
    delete = _spy("delete")
    put_blob = _spy("put_blob")
    get_blob = _spy("get_blob")
    prune_receipts = _spy("prune_receipts")


def _settings(tmp_path, **extra):
    return make_settings(
        {"paths.threads_data_path": str(tmp_path / "threads"), **extra}
    )


async def _open(tmp_path, store, **extra) -> ThreadSession:
    return await ThreadSession.afrom_config(
        settings=_settings(tmp_path, **extra),
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=store,
        claim=True,
    )


def _msg(i: str) -> dict:
    return {"id": i, "role": "user", "content": f"текст {i}"}


async def _step(ts: ThreadSession, *messages: dict) -> None:
    await ts.apersist_batch(
        list(messages),
        previous_response_id=None,
        session=ts.state,
        usage=ts.usage,
    )


async def test_step_is_one_commit_without_read(tmp_path):
    spy = SpyStore(MemoryThreadStore())
    ts = await _open(tmp_path, spy)
    spy.calls.clear()
    await _step(ts, _msg("m1"))
    await _step(ts, _msg("m2"))
    assert spy.calls == ["commit", "commit"]
    ids = [e.data["id"] for e in await spy.inner.read_stream(KEY, "messages")]
    assert ids == ["m1", "m2"]


async def test_repeat_of_written_message_is_not_duplicated(tmp_path):
    store = MemoryThreadStore()
    ts = await _open(tmp_path, store)
    await _step(ts, _msg("m1"))
    await _step(ts, _msg("m1"), _msg("m2"))
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["m1", "m2"]
    assert ts.persisted_messages_count == 2


async def test_runtime_flag_is_a_side_write_without_read(tmp_path):
    spy = SpyStore(MemoryThreadStore())
    ts = await _open(tmp_path, spy)
    spy.calls.clear()
    await ts.aset_flag("summarizing", active=True)
    await ts.aset_flag("summarizing", active=False)
    await ts.amark_turn(running=True)
    assert spy.calls == ["append_side", "append_side", "append_side"]
    record = await spy.inner.get_thread(KEY)
    assert record.runtime.turn == "running"
    assert not record.runtime.flags


async def test_compaction_is_one_commit(tmp_path):
    spy = SpyStore(MemoryThreadStore())
    ts = await _open(tmp_path, spy)
    await _step(ts, _msg("m1"), _msg("m2"))
    spy.calls.clear()
    result = await ts.aarchive_and_compact_summary_payload(
        {"summary_message_content": "сводка", "dialog_steps_offset": 2}
    )
    assert spy.calls.count("commit") == 1
    assert "get_thread" not in spy.calls
    record = await spy.inner.get_thread(KEY)
    assert record.state.dialog_steps_offset == 2
    assert record.state.last_summarized_at
    assert len(record.archives) == 1
    assert result["new_messages_count"] == 1
    assert ts.persisted_messages_count == 1


@pytest.mark.parametrize("kind", ["file", "memory"])
async def test_session_checks_pass(tmp_path, kind):
    """Общий набор пути хода (чужие секции, сжатие, повторное открытие) —
    его же гоняют пакеты хранилищ на своих базах."""
    made = iter(range(100))

    def make():
        if kind == "file":
            return FileThreadStore(tmp_path / f"files{next(made)}")
        return MemoryThreadStore()

    passed = await aassert_session_works(make)
    assert passed == [check.__name__ for check in SESSION_CHECKS]


async def test_new_thread_first_step_matches_develop(tmp_path):
    """Новый тред: created_at ставит хранилище, messages_count — по
    истории, api_mode — из сессии (Review Focus 4)."""
    store = FileThreadStore(tmp_path / "files")
    ts = await _open(tmp_path, store)
    ts.backend_api_mode = "chat_completions"
    await _step(ts, _msg("m1"), _msg("m2"))
    meta = meta_from_record(await store.get_thread(KEY))
    assert meta["messages_count"] == 2
    assert meta["backend_api_mode"] == "chat_completions"
    assert meta["created_at"]


async def test_persist_turn_flush_retry_no_duplicates(tmp_path):
    """persist: turn — шаги копятся; сбой посреди сброса и повтор не
    задваивают историю (Review Focus 3)."""
    store = MemoryThreadStore()
    ts = await _open(tmp_path, store)
    ts._defer_persist = True
    await _step(ts, _msg("m1"), _msg("m2"))
    await _step(ts, _msg("m3"))
    real = store.commit
    calls: list = []

    async def fail_once(key, change):
        calls.append(change)
        if len(calls) == 1:
            raise RuntimeError("сбой посреди сброса")
        return await real(key, change)

    store.commit = fail_once
    with pytest.raises(RuntimeError):
        await ts.aflush_deferred()
    await ts.aflush_deferred()
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["m1", "m2", "m3"]
