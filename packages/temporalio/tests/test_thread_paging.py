"""Тред и трасса субагента читаются страницами меньше лимита записи Temporal.

Результат activity — запись истории Temporal (лимит 2 МБ). Тред любой длины
читается несколькими activities; здесь — без сервера: прокси workflow
вызывает activity напрямую, размер каждого ответа меряется штатным
конвертером Temporal.
"""

import pytest
from gc_core.execution.contracts import ExecutionFailure
from gc_core.thread_store_durable import ThreadStoreConflict
from gc_core.thread_store_file_durable import DurableFileThreadStore
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from gc_core_temporalio.thread_store_activities import (
    ThreadStoreActivities,
    _state_page,
)
from gc_core_temporalio.thread_store_contracts import PAGE_BYTES
from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
from temporalio.converter import DataConverter

#: Лимит Temporal на одну запись истории.
TEMPORAL_PAYLOAD_LIMIT = 2 * 1024 * 1024


def _store(kind, tmp_path):
    if kind == "file":
        return DurableFileThreadStore(str(tmp_path / "threads"))
    return SQLiteThreadStore(str(tmp_path / "threads.sqlite"))


def _fill(store, total_bytes, *, message_bytes=50_000, thread="t"):
    messages, size, index = [], 0, 0
    while size < total_bytes:
        messages.append(
            {
                "id": f"m{index}",
                "role": "user" if index % 2 == 0 else "assistant",
                "content": f"{index}:" + "x" * message_bytes,
            }
        )
        size += message_bytes
        index += 1
    store.append_messages("s", thread, messages)
    return messages


class _Recorder:
    """Транспорт прокси без сервера: вызывает activity и меряет ответы."""

    def __init__(self, activities, before_call=None):
        self.activities = activities
        self.sizes = []
        self.calls = 0
        self.before_call = before_call

    async def __call__(self, name, request):
        self.calls += 1
        if self.before_call is not None:
            self.before_call(self.calls, request)
        handler = {
            "universal_agent.thread.load": self.activities.load,
            "universal_agent.thread.read_trace": self.activities.read_trace,
        }[name]
        try:
            reply = await handler(request)
        except Exception as exc:
            # Как ThreadStoreTransport: тип ошибки хранилища — по имени.
            if getattr(exc, "type", None) == "ThreadStoreConflict":
                raise ThreadStoreConflict(str(exc)) from exc
            raise
        payload = DataConverter.default.payload_converter.to_payloads([reply])
        self.sizes.append(payload[0].ByteSize())
        return reply


def _proxy(recorder):
    return TemporalThreadStore("files", recorder)


@pytest.mark.parametrize("kind", ["file", "sqlite"])
@pytest.mark.parametrize("megabytes", [5, 20])
async def test_large_thread_is_read_in_pages_below_the_payload_limit(
    tmp_path, kind, megabytes
):
    store = _store(kind, tmp_path)
    expected = _fill(store, megabytes * 1024 * 1024)
    recorder = _Recorder(ThreadStoreActivities({"files": store}))
    snapshot = await _proxy(recorder).aread_snapshot("s", "t")
    direct = store.read_snapshot("s", "t")
    assert snapshot.state.messages == direct.state.messages
    assert [m["id"] for m in snapshot.state.messages] == [
        m["id"] for m in expected
    ]
    assert snapshot.state.known_keys == set(direct.state.known_keys)
    assert snapshot.state.max_seq == direct.state.max_seq
    assert snapshot.revision == direct.revision
    # Каждая страница — отдельная запись истории меньше лимита Temporal.
    assert recorder.calls >= megabytes
    assert max(recorder.sizes) < TEMPORAL_PAYLOAD_LIMIT
    assert max(recorder.sizes) <= PAGE_BYTES + 200_000


async def test_one_message_close_to_the_limit_is_its_own_page(tmp_path):
    store = _store("file", tmp_path)
    store.append_messages(
        "s",
        "t",
        [
            {"id": "small", "role": "user", "content": "hi"},
            {"id": "big", "role": "assistant", "content": "y" * 1_500_000},
            {"id": "tail", "role": "user", "content": "bye"},
        ],
    )
    recorder = _Recorder(ThreadStoreActivities({"files": store}))
    snapshot = await _proxy(recorder).aread_snapshot("s", "t")
    assert [m["id"] for m in snapshot.state.messages] == [
        "small",
        "big",
        "tail",
    ]
    assert max(recorder.sizes) < TEMPORAL_PAYLOAD_LIMIT


async def test_pages_come_from_one_snapshot_on_the_same_worker(tmp_path):
    """Запись между страницами не рвёт чтение: страницы того же воркера —
    из одного снимка (тред на момент первой страницы, как одно чтение)."""
    store = _store("file", tmp_path)
    _fill(store, 3 * 1024 * 1024)
    before = store.read_snapshot("s", "t").state.messages

    def write_once(call, request):
        if call == 2:
            store.append_messages(
                "s", "t", [{"id": "late", "role": "user", "content": "late"}]
            )

    recorder = _Recorder(
        ThreadStoreActivities({"files": store}), before_call=write_once
    )
    snapshot = await _proxy(recorder).aread_snapshot("s", "t")
    assert snapshot.state.messages == before


async def test_changed_thread_on_another_worker_is_read_again(tmp_path):
    """Страницу обслужил другой воркер (без кэша), а тред изменился —
    чтение начинается заново и отдаёт новую версию целиком."""
    store = _store("file", tmp_path)
    _fill(store, 3 * 1024 * 1024)
    activities = ThreadStoreActivities({"files": store})

    def other_worker(call, request):
        activities._snapshots.clear()
        if call == 2:
            store.append_messages(
                "s", "t", [{"id": "late", "role": "user", "content": "late"}]
            )

    recorder = _Recorder(activities, before_call=other_worker)
    snapshot = await _proxy(recorder).aread_snapshot("s", "t")
    assert (
        snapshot.state.messages == store.read_snapshot("s", "t").state.messages
    )
    assert snapshot.state.messages[-1]["id"] == "late"


async def test_thread_changing_on_every_read_gives_up_with_conflict(tmp_path):
    store = _store("file", tmp_path)
    _fill(store, 3 * 1024 * 1024)
    activities = ThreadStoreActivities({"files": store})

    def write_always(call, request):
        activities._snapshots.clear()
        if request.cursor is not None:
            store.append_messages(
                "s",
                "t",
                [{"id": f"late{call}", "role": "user", "content": "late"}],
            )

    recorder = _Recorder(activities, before_call=write_always)
    with pytest.raises(ThreadStoreConflict):
        await _proxy(recorder).aread_snapshot("s", "t")


async def test_meta_only_read_is_one_small_call(tmp_path):
    store = _store("file", tmp_path)
    _fill(store, 5 * 1024 * 1024)
    recorder = _Recorder(ThreadStoreActivities({"files": store}))
    snapshot = await _proxy(recorder).aread_snapshot(
        "s", "t", include_state=False
    )
    assert snapshot.state is None
    assert recorder.calls == 1 and recorder.sizes[0] < 10_000


async def test_large_subagent_trace_is_read_in_pages(tmp_path):
    store = _store("file", tmp_path)
    for index in range(120):
        store.append_subagent_trace(
            "s",
            "t",
            parent_call_id="call",
            child_run_id="child",
            kind="message",
            payload={"index": index, "text": "z" * 40_000},
        )
    recorder = _Recorder(ThreadStoreActivities({"files": store}))
    trace = await _proxy(recorder).aread_subagent_trace("s", "t")
    assert [record["payload"]["index"] for record in trace] == list(range(120))
    assert recorder.calls >= 4
    assert max(recorder.sizes) < TEMPORAL_PAYLOAD_LIMIT


def test_state_page_walks_messages_then_keys_and_never_returns_empty():
    messages = [{"id": str(i), "content": "a" * 300} for i in range(10)]
    keys = [f"k{i}" for i in range(10)]
    seen_messages, seen_keys, cursor, pages = [], [], None, 0
    while True:
        page = _state_page(messages, keys, cursor, 1000)
        assert page.messages or page.keys
        seen_messages += page.messages
        seen_keys += page.keys
        pages += 1
        cursor = page.next_cursor
        if cursor is None:
            break
    assert seen_messages == messages and seen_keys == keys
    assert pages > 3
    empty = _state_page([], [], None, 1000)
    assert empty.messages == [] and empty.keys == []
    assert empty.next_cursor is None


async def test_missing_state_keeps_the_old_error(tmp_path):
    from dataclasses import replace

    class NoState(DurableFileThreadStore):
        def read_snapshot(self, system_id, thread_id, *, include_state=True):
            snapshot = super().read_snapshot(system_id, thread_id)
            return replace(snapshot, state=None)

    recorder = _Recorder(
        ThreadStoreActivities({"files": NoState(str(tmp_path / "threads"))})
    )
    with pytest.raises(ExecutionFailure):
        await _proxy(recorder).aload_thread_state("s", "t")
