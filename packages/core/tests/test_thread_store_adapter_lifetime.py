"""Адаптер синхронного хранилища тредов: не держит хранилища и не стирает трассу.

Две находки ревью. Кэш адаптеров был `WeakKeyDictionary`, но значение (адаптер)
держало ключ (хранилище) сильной ссылкой — из 1000 выброшенных хранилищ в кэше
оставались все 1000. И адаптер не проксировал `replace_messages`: компакция
синхронного хранилища без `archive_and_replace` уходила в `reset` + `append`, а
`reset` по контракту сносит трассу субагентов.
"""

import gc
import weakref

from gc_core.thread_store_async import athread_store
from gc_testkit.memory_thread_store import MemoryThreadStore


def test_dropped_stores_do_not_stay_in_the_adapter_cache():
    alive = weakref.WeakSet()
    for _ in range(200):
        alive.add(athread_store(MemoryThreadStore()))
    gc.collect()
    gc.collect()
    assert len(alive) == 0, (
        f"адаптеров пережило сборку мусора: {len(alive)} из 200"
    )


def test_the_same_store_gets_the_same_adapter():
    store = MemoryThreadStore()
    assert athread_store(store) is athread_store(store)


async def test_replace_keeps_the_subagent_trace_of_a_sync_store():
    port = athread_store(MemoryThreadStore())
    await port.aappend_messages(
        "sys", "t1", [{"role": "user", "content": "было"}]
    )
    await port.aappend_subagent_trace(
        "sys",
        "t1",
        parent_call_id="c1",
        child_run_id="r1",
        kind="user",
        payload={"content": "вопрос ребёнку"},
    )
    replace = getattr(port, "areplace_messages", None)
    assert callable(replace), (
        "адаптер не проксирует replace_messages — компакция уйдёт в reset"
    )
    await replace("sys", "t1", [{"role": "user", "content": "сводка"}])

    trace = await port.aread_subagent_trace("sys", "t1")
    assert [r["kind"] for r in trace] == ["user"], (
        "компакция стёрла трассу субагентов"
    )
    messages = await port.aload_messages("sys", "t1")
    assert [m.get("content") for m in messages] == ["сводка"]
