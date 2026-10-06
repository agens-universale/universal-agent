"""persist: turn — шаги копятся и пишутся в точках сброса; локально запрещено."""

from types import SimpleNamespace

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_testkit.settings import fake_ctx, make_settings


class CountingStore(DurableFileThreadStore):
    def __init__(self, path):
        super().__init__(path)
        self.commits = []

    def commit_operation(self, system_id, thread_id, operation):
        self.commits.append(operation.kind)
        return super().commit_operation(system_id, thread_id, operation)


async def _session(store, persist):
    return await ThreadSession.afrom_config(
        settings=make_settings({"thread_store.persist": persist}),
        system_id="cli",
        thread_id="t",
        store=store,
    )


def _message(index):
    return {"id": f"m{index}", "role": "user", "content": f"step {index}"}


async def test_turn_mode_writes_all_steps_in_one_commit(tmp_path):
    store = CountingStore(str(tmp_path))
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
    assert [m["id"] for m in store.load_messages("cli", "t")] == [
        "m0",
        "m1",
        "m2",
    ]
    assert store.load_meta("cli", "t")["messages_count"] == 3


async def test_failed_flush_keeps_steps_for_retry(tmp_path):
    """Сбой коммита не теряет ход: буфер чистится только после записи."""
    store = CountingStore(str(tmp_path))
    session = await _session(store, "turn")
    for index in range(2):
        await session.apersist_batch(
            [_message(index)], previous_response_id=None
        )
    commit = store.commit_operation
    store.commit_operation = lambda *args: (_ for _ in ()).throw(
        RuntimeError("store is down")
    )
    with pytest.raises(RuntimeError):
        await session.aflush_deferred()
    store.commit_operation = commit
    await session.aflush_deferred()
    assert [m["id"] for m in store.load_messages("cli", "t")] == ["m0", "m1"]
    await session.aflush_deferred()
    assert store.commits == ["persist"]


async def test_step_mode_keeps_one_commit_per_step(tmp_path):
    store = CountingStore(str(tmp_path))
    session = await _session(store, "step")
    for index in range(3):
        await session.apersist_batch(
            [_message(index)], previous_response_id=None
        )
    assert store.commits == ["persist"] * 3


async def test_compaction_flushes_deferred_steps_first(tmp_path):
    store = CountingStore(str(tmp_path))
    session = await _session(store, "turn")
    await session.apersist_batch([_message(0)], previous_response_id=None)
    await session.aarchive_and_compact_summary_payload(
        {"summary_message_content": "compressed", "dialog_steps_offset": 1}
    )
    assert store.commits == ["persist", "compact"]
    archived = list(tmp_path.glob("data_cli_t__pre_summary__*.jsonl"))
    assert archived and "step 0" in archived[0].read_text()


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


async def test_store_with_operation_limit_gets_the_turn_in_parts(tmp_path):
    """Durable-исполнитель ограничивает запись (у Temporal — 2 МБ на запись
    истории): хранилище с ``max_operation_bytes`` получает ход частями, по
    порядку, без потерь и дублей. Без потолка — одной операцией."""
    store = CountingStore(str(tmp_path))
    store.max_operation_bytes = 3_000
    session = await _session(store, "turn")
    for index in range(10):
        await session.apersist_batch(
            [{"id": f"m{index}", "role": "user", "content": "x" * 1_000}],
            previous_response_id=None,
        )
    await session.aflush_deferred()
    assert store.commits == ["persist"] * 5  # по 2 сообщения (~1 КБ каждое)
    assert [m["id"] for m in store.load_messages("cli", "t")] == [
        f"m{index}" for index in range(10)
    ]
    assert store.load_meta("cli", "t")["messages_count"] == 10
    await session.aflush_deferred()
    assert len(store.commits) == 5


async def test_part_failure_keeps_only_unwritten_steps(tmp_path):
    store = CountingStore(str(tmp_path))
    store.max_operation_bytes = 3_000
    session = await _session(store, "turn")
    for index in range(6):
        await session.apersist_batch(
            [{"id": f"m{index}", "role": "user", "content": "x" * 1_000}],
            previous_response_id=None,
        )
    original = store.commit_operation
    calls = []

    def fail_second(system_id, thread_id, operation):
        calls.append(1)
        if len(calls) == 2:
            raise RuntimeError("store down")
        return original(system_id, thread_id, operation)

    store.commit_operation = fail_second
    with pytest.raises(RuntimeError):
        await session.aflush_deferred()
    # Записана первая часть; вторая и дальше остались в буфере.
    assert [m["id"] for m in store.load_messages("cli", "t")] == ["m0", "m1"]
    store.commit_operation = original
    await session.aflush_deferred()
    assert [m["id"] for m in store.load_messages("cli", "t")] == [
        f"m{index}" for index in range(6)
    ]
