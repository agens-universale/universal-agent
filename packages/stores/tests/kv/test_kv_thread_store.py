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
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works

KEY = ThreadKey(system="cli", thread="t")
TTL = timedelta(minutes=5)


def test_kv_store_conforms():
    def make(clock):
        return KvThreadStore(MemoryKV(), clock=clock)

    assert assert_conforms(make) == [check.__name__ for check in CHECKS]


async def test_session_over_kv_works():
    passed = await aassert_session_works(lambda: KvThreadStore(MemoryKV()))
    assert passed == [check.__name__ for check in SESSION_CHECKS]


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


class _Clock:
    def __init__(self) -> None:
        from datetime import UTC, datetime

        self.now = datetime(2026, 10, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


async def _deleted_thread(store, key):
    await store.append_side(
        key,
        SideChange(op_id=f"make-{key.thread}", patch_frontend=("t", {"x": 1})),
    )
    await store.delete(key, op_id=f"drop-{key.thread}")


async def test_old_tombstones_are_reaped_by_maintenance():
    """Ревью, раунд 4: заголовки-надгробия удалённых тредов не убирал
    никто — chat completions оставлял по одному на запрос. Обслуживание
    убирает надгробия старше суток (как SQL), свежие — фенсинг — живут."""
    from gc_stores.kv import keys

    kv, clock = MemoryKV(), _Clock()
    store = KvThreadStore(kv, clock=clock)
    old = ThreadKey(system="completions", thread="old")
    fresh = ThreadKey(system="completions", thread="fresh")
    await _deleted_thread(store, old)
    clock.now += timedelta(days=1, minutes=1)
    await _deleted_thread(store, fresh)
    await store.prune_receipts(timedelta(hours=24))
    assert await kv.get(keys.head_key(old)) is None
    assert await kv.get(keys.head_key(fresh)) is not None


async def test_reaping_spares_a_thread_recreated_meanwhile():
    """Тред пересоздали между чтением надгробия и его удалением — живой
    заголовок не удаляется (удаление по версии)."""
    from gc_stores.kv import keys

    clock = _Clock()
    recreated: dict = {}

    class Racing(MemoryKV):
        async def delete_if(self, key, version):
            if key.endswith("/h") and not recreated:
                recreated["done"] = True
                await store.append_side(
                    KEY,
                    SideChange(op_id="again", patch_frontend=("t", {"y": 2})),
                )
            return await super().delete_if(key, version)

    kv = Racing()
    store = KvThreadStore(kv, clock=clock)
    await _deleted_thread(store, KEY)
    clock.now += timedelta(days=2)
    await store.prune_receipts(timedelta(hours=24))
    assert recreated
    assert await kv.get(keys.head_key(KEY)) is not None
    assert (await store.get_thread(KEY)).frontend == {"t": {"y": 2}}
