"""Целостность KV-движка там, где ключи живут дольше записи (находки
проверки безопасности): квитанции удалённого треда, резервы алиасов при
медленной записи и при удалении."""

import contextlib
import json
from datetime import UTC, datetime, timedelta

import pytest
from gc_core.threads.models import (
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
)
from gc_stores.kv import keys
from gc_stores.kv.memory import KvFault, MemoryKV
from gc_stores.kv.thread_store import ALIAS_HOLD, KvThreadStore

KEY = ThreadKey(system="cli", thread="a")
OTHER = ThreadKey(system="cli", thread="b")
TTL = timedelta(minutes=5)


class Clock:
    def __init__(self):
        self.now = datetime(2026, 1, 1, tzinfo=UTC)

    def __call__(self):
        return self.now


async def _commit(store, key, op_id, revision, lease, **extra):
    return await store.commit(
        key,
        ThreadChange(
            op_id=op_id, expected_revision=revision, lease=lease, **extra
        ),
    )


async def test_receipt_of_deleted_thread_does_not_replay_in_new_one():
    """Уборка после удаления не прошла — квитанции старого треда остались;
    тред заново под тем же ключом их не видит: запись применяется."""
    kv = MemoryKV()
    store = KvThreadStore(kv)
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    await _commit(
        store, KEY, "x", 0, lease, append_messages=[{"content": "старое"}]
    )
    await _commit(store, KEY, "y", 1, lease)  # квитанция x — в объекте
    kv.fail_on = lambda op, key: op == "delete"
    await store.delete(KEY, op_id="d")  # уборка падает, удаление — нет
    kv.fail_on = None
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    receipt = await _commit(
        store, KEY, "x", 0, lease, append_messages=[{"content": "новое"}]
    )
    assert receipt.revision == 1
    contents = [
        e.data["content"] for e in await store.read_stream(KEY, "messages")
    ]
    assert contents == ["новое"]


async def test_own_reservation_is_refreshed_when_reused():
    clock = Clock()
    kv = MemoryKV()
    store = KvThreadStore(kv, clock=clock)
    kv.fail_on = lambda op, key: op == "put" and key.endswith("/h")
    with contextlib.suppress(KvFault):
        await store.append_side(KEY, SideChange(op_id="s", add_aliases=["al"]))
    kv.fail_on = None
    clock.now += ALIAS_HOLD * 3
    await store.append_side(KEY, SideChange(op_id="s", add_aliases=["al"]))
    record = json.loads((await kv.get(keys.alias_key("al"))).data)
    assert datetime.fromisoformat(record["at"]) == clock.now


async def test_delete_keeps_an_alias_that_another_thread_owns_now():
    kv = MemoryKV()
    store = KvThreadStore(kv)
    await store.append_side(KEY, SideChange(op_id="s1", add_aliases=["al"]))
    await store.append_side(OTHER, SideChange(op_id="s2"))
    # Резерв перешёл к другому треду (перехват сироты), а заголовок KEY
    # всё ещё перечисляет алиас: удаление KEY не трогает чужой резерв.
    await kv.put_any(
        keys.alias_key("al"),
        json.dumps(
            {
                "system": "cli",
                "thread": "b",
                "at": datetime.now(UTC).isoformat(),
            }
        ).encode(),
    )
    head = await kv.get(keys.head_key(OTHER))
    other = json.loads(head.data)
    other["aliases"] = ["al"]
    await kv.put_any(keys.head_key(OTHER), json.dumps(other).encode())
    await store.delete(KEY, op_id="d")
    assert await store.resolve_alias("al") == OTHER


class _TakeoverKV(MemoryKV):
    """Пока запись пишет куски, резерв алиаса перехватывает другой тред."""

    takeover = False

    async def put_any(self, key, data):
        if self.takeover and "/c/" in key:
            self.takeover = False
            record = {
                "system": "cli",
                "thread": "b",
                "at": "2099-01-01T00:00:00+00:00",
            }
            await super().put_any(
                keys.alias_key("al"), json.dumps(record).encode()
            )
        return await super().put_any(key, data)


async def test_alias_lost_during_a_slow_commit_is_not_listed():
    kv = _TakeoverKV()
    store = KvThreadStore(kv)
    lease = (await store.claim(KEY, LeaseRequest(owner="w", ttl=TTL))).lease
    kv.takeover = True
    with pytest.raises(ValueError, match="принадлежит"):
        await _commit(
            store,
            KEY,
            "o",
            0,
            lease,
            add_aliases=["al"],
            append_messages=[{"content": "x"}],
        )
    head = json.loads((await kv.get(keys.head_key(KEY))).data)
    assert "al" not in head["aliases"]
