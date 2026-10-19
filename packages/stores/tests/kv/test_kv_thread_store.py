"""``KvThreadStore`` на ``MemoryKV``: конформанс контракта v2 целиком и
инвариант движка — прерванная запись не оставляет видимых следов, повтор с
тем же ``op_id`` её доводит."""

import contextlib
from datetime import timedelta

import pytest
from gc_core.threads.errors import ThreadConflict
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadQuery,
)
from gc_stores.kv.memory import KvFault, MemoryKV
from gc_stores.kv.thread_store import KvThreadStore
from gc_testkit.thread_store_conformance_v2 import CHECKS, assert_conforms

KEY = ThreadKey(system="cli", thread="t")
TTL = timedelta(minutes=5)


def test_kv_store_conforms():
    def make(clock):
        return KvThreadStore(MemoryKV(), clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


def _crash_at(operation: str, marker: str):
    def fail(op: str, key: str) -> bool:
        return op == operation and marker in key

    return fail


@pytest.mark.parametrize(
    ("operation", "marker"),
    [
        ("put_any", "/c/"),  # кусок потока до фиксации
        ("put_any", "idx/"),  # ключ индекса до фиксации
        ("put", "/h"),  # сама фиксация (заголовок)
        ("delete", "idx/"),  # уборка старого ключа индекса после
    ],
)
async def test_crash_at_any_write_point_leaves_thread_whole(operation, marker):
    kv = MemoryKV()
    store = KvThreadStore(kv)
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    first = ThreadChange(
        op_id="o1",
        expected_revision=0,
        lease=lease,
        append_messages=[{"content": "раз"}],
    )
    await store.commit(KEY, first)
    change = ThreadChange(
        op_id="o2",
        expected_revision=1,
        lease=lease,
        append_messages=[{"content": "два"}],
    )
    kv.fail_on = _crash_at(operation, marker)
    with contextlib.suppress(KvFault):
        await store.commit(KEY, change)
    kv.fail_on = None
    visible = [
        e.data["content"] for e in await store.read_stream(KEY, "messages")
    ]
    assert visible in (["раз"], ["раз", "два"]), "запись видна наполовину"
    receipt = await store.commit(KEY, change)  # повтор после «падения»
    assert receipt.revision == 2
    contents = [
        e.data["content"] for e in await store.read_stream(KEY, "messages")
    ]
    assert contents == ["раз", "два"]
    page = await store.list_threads(ThreadQuery())
    assert [r.key for r in page.items] == [KEY]


async def test_rejected_commit_leaves_no_visible_alias():
    store = KvThreadStore(MemoryKV())
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    with pytest.raises(ThreadConflict):
        await store.commit(
            KEY,
            ThreadChange(
                op_id="o", expected_revision=7, lease=lease, add_aliases=["al"]
            ),
        )
    assert await store.resolve_alias("al") is None


async def test_alias_and_op_id_may_be_any_string():
    store = KvThreadStore(MemoryKV())
    strange = ["a/b", "a|b", "я", "%20", "x" * 300, "dup", "dup "]
    for i, alias in enumerate(strange):
        key = ThreadKey(system="s", thread=f"t{i}")
        await store.append_side(
            key, SideChange(op_id=alias, add_aliases=[alias])
        )
    for i, alias in enumerate(strange):
        assert await store.resolve_alias(alias) == ThreadKey(
            system="s", thread=f"t{i}"
        )


async def test_sweep_removes_orphans_only():
    kv = MemoryKV()
    store = KvThreadStore(kv)
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    await store.commit(
        KEY,
        ThreadChange(
            op_id="o1",
            expected_revision=0,
            lease=lease,
            append_messages=[{"content": "живое"}],
        ),
    )
    kv.fail_on = _crash_at("put", "/h")
    with contextlib.suppress(KvFault):
        await store.commit(
            KEY,
            ThreadChange(
                op_id="o2",
                expected_revision=1,
                lease=lease,
                append_messages=[{"content": "сирота"}],
                add_aliases=["сирота"],
            ),
        )
    kv.fail_on = None
    before = kv.keys()
    removed = await store.sweep(timedelta(0))
    assert removed >= 2  # кусок и ключ индекса сироты (и резерв алиаса)
    assert len(kv.keys()) == len(before) - removed
    contents = [
        e.data["content"] for e in await store.read_stream(KEY, "messages")
    ]
    assert contents == ["живое"]
    assert [
        r.key for r in (await store.list_threads(ThreadQuery())).items
    ] == [KEY]
