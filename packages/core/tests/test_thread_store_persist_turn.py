"""persist: turn — шаги копятся и пишутся в точках сброса; локально запрещено.

Хранилище — контракт v2 (под Temporal тред пишется через ``SessionPort``):
режим хода включает настройка, а не особая способность хранилища.
"""

from types import SimpleNamespace

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.models import ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import fake_ctx, make_settings

KEY = ThreadKey(system="cli", thread="t")


def _kind(change) -> str:
    if change.replace_history is not None:
        return "compact"
    if change.append_messages:
        return "persist"
    return "meta"


class CountingStore(MemoryThreadStore):
    """Записи истории по видам; ``fail`` — номера записей (с 1), на которых
    хранилище «лежит»."""

    def __init__(self) -> None:
        super().__init__()
        self.commits: list[str] = []
        self.fail: set[int] = set()
        self._calls = 0

    async def commit(self, key, change):
        self._calls += 1
        if self._calls in self.fail:
            raise RuntimeError("store is down")
        receipt = await super().commit(key, change)
        self.commits.append(_kind(change))
        return receipt


async def _session(store, persist):
    return await ThreadSession.afrom_config(
        settings=make_settings({"thread_store.persist": persist}),
        system_id="cli",
        thread_id="t",
        store=store,
    )


def _message(index, content=None):
    return {
        "id": f"m{index}",
        "role": "user",
        "content": content or f"step {index}",
    }


async def _ids(store) -> list[str]:
    return [e.data["id"] for e in await store.read_stream(KEY, "messages")]


async def test_turn_mode_writes_all_steps_in_one_commit():
    store = CountingStore()
    session = await _session(store, "turn")
    for index in range(3):
        await session.apersist_batch(
            [_message(index)], previous_response_id=None
        )
    assert store.commits == []
    assert [m["id"] for m in session.get_full_history_messages()] == [
        "m0",
        "m1",
        "m2",
    ]
    await session.aflush_deferred()
    await session.aflush_deferred()
    assert store.commits == ["persist"]
    assert await _ids(store) == ["m0", "m1", "m2"]
    assert (await store.get_thread(KEY)).info.messages_count == 3


async def test_failed_flush_keeps_steps_for_retry():
    """Сбой записи не теряет ход: буфер чистится только после записи."""
    store = CountingStore()
    session = await _session(store, "turn")
    for index in range(2):
        await session.apersist_batch(
            [_message(index)], previous_response_id=None
        )
    store.fail = {1}
    with pytest.raises(RuntimeError):
        await session.aflush_deferred()
    await session.aflush_deferred()
    assert await _ids(store) == ["m0", "m1"]
    await session.aflush_deferred()
    assert store.commits == ["persist"]


async def test_step_mode_keeps_one_commit_per_step():
    store = CountingStore()
    session = await _session(store, "step")
    for index in range(3):
        await session.apersist_batch(
            [_message(index)], previous_response_id=None
        )
    assert store.commits == ["persist"] * 3


async def test_compaction_flushes_deferred_steps_first():
    store = CountingStore()
    session = await _session(store, "turn")
    await session.apersist_batch([_message(0)], previous_response_id=None)
    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "compressed", "dialog_steps_offset": 1}
    )
    # Несохранённый шаг — сначала; затем одной записью история заменяется
    # сводкой вместе с состоянием после сжатия.
    assert store.commits == ["persist", "compact"]
    record = await store.get_thread(KEY)
    archived = await store.read_archive(KEY, record.archives[0].id)
    assert [e.data["content"] for e in archived] == ["step 0"]


async def test_local_executor_rejects_turn_mode():
    from gc_core.agent_executor import AgentExecutor

    ctx = fake_ctx({"thread_store.persist": "turn"})
    with pytest.raises(ValueError, match="durable executor"):
        await AgentExecutor.arun(
            SimpleNamespace(_ctx=ctx, _lock=_NoLock(), _state=None),
            ["hello"],
        )


class _NoLock:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False


async def test_store_with_operation_limit_gets_the_turn_in_parts():
    """Durable-исполнитель ограничивает запись (у Temporal — 2 МБ на запись
    истории): хранилище с ``max_operation_bytes`` получает ход частями, по
    порядку, без потерь и дублей. Без потолка — одной записью."""
    store = CountingStore()
    store.max_operation_bytes = 3_000
    session = await _session(store, "turn")
    for index in range(10):
        await session.apersist_batch(
            [_message(index, "x" * 1_000)], previous_response_id=None
        )
    await session.aflush_deferred()
    assert store.commits == ["persist"] * 5  # по 2 сообщения (~1 КБ каждое)
    assert await _ids(store) == [f"m{index}" for index in range(10)]
    assert (await store.get_thread(KEY)).info.messages_count == 10
    await session.aflush_deferred()
    assert len(store.commits) == 5


async def test_part_failure_keeps_only_unwritten_steps():
    store = CountingStore()
    store.max_operation_bytes = 3_000
    session = await _session(store, "turn")
    for index in range(6):
        await session.apersist_batch(
            [_message(index, "x" * 1_000)], previous_response_id=None
        )
    store.fail = {2}
    with pytest.raises(RuntimeError):
        await session.aflush_deferred()
    # Записана первая часть; вторая и дальше остались в буфере.
    assert await _ids(store) == ["m0", "m1"]
    await session.aflush_deferred()
    assert await _ids(store) == [f"m{index}" for index in range(6)]
