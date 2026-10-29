"""Примитивы KV-движка: раскладка ключей и ``MemoryKV``."""

from datetime import UTC, datetime

import pytest
from gc_core.threads.models import ThreadKey
from gc_stores.kv import keys
from gc_stores.kv.memory import KvFault, MemoryKV


@pytest.mark.parametrize(
    ("a", "b"),
    [
        ("a", "ab"),
        ("ab", "b"),
        ("Z", "a"),
        ("я", "яя"),
        ("x/y", "x|y"),
        ("%", "&"),
        ("", "a"),
    ],
)
def test_encoded_order_is_codepoint_order(a, b):
    assert (keys.enc(a) < keys.enc(b)) == (a < b)
    assert keys.dec(keys.enc(a)) == a


def test_index_orders_newest_first_then_system_and_thread():
    older = datetime(2026, 1, 1, tzinfo=UTC)
    newer = datetime(2026, 1, 2, 3, 4, 5, 678901, tzinfo=UTC)
    first = keys.index_key(older, ThreadKey(system="a", thread="x"))
    second = keys.index_key(newer, ThreadKey(system="b", thread="y"))
    third = keys.index_key(newer, ThreadKey(system="a", thread="z"))
    assert sorted([first, second, third]) == [third, second, first]
    assert keys.index_entry(second) == (
        newer,
        ThreadKey(system="b", thread="y"),
    )


async def test_conditional_put():
    kv = MemoryKV()
    v1 = await kv.put("k", b"1", if_version=None)
    assert v1 is not None
    assert await kv.put("k", b"2", if_version=None) is None
    assert await kv.put("k", b"2", if_version="чужая") is None
    assert await kv.put("k", b"2", if_version=v1) is not None
    assert (await kv.get("k")).data == b"2"
    await kv.delete("k")
    assert await kv.get("k") is None


async def test_scan_is_sorted_and_paged():
    kv = MemoryKV()
    for key in ("p/b", "p/a", "q/a", "p/c", "o/z"):
        await kv.put_any(key, b"")
    assert await kv.scan("p/") == ["p/a", "p/b", "p/c"]
    assert await kv.scan("p/", start_after="p/a", limit=1) == ["p/b"]
    assert await kv.scan("p/", start_after="a") == ["p/a", "p/b", "p/c"]
    assert await kv.scan("p/", start_after="p/c") == []


async def test_fault_stops_before_the_operation():
    kv = MemoryKV()
    kv.fail_on = lambda operation, key: operation == "put_any"
    with pytest.raises(KvFault):
        await kv.put_any("k", b"x")
    assert await kv.get("k") is None


async def test_conditional_delete():
    """Удаление по версии: ключ, переписанный после чтения, остаётся."""
    kv = MemoryKV()
    assert kv.conditional_delete
    v1 = await kv.put("k", b"1", if_version=None)
    v2 = await kv.put("k", b"2", if_version=v1)
    assert await kv.delete_if("k", v1) is False
    assert (await kv.get("k")).data == b"2"
    assert await kv.delete_if("k", v2) is True
    assert await kv.get("k") is None
    assert await kv.delete_if("k", v2) is False
