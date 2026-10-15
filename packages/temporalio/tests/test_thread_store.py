"""Провод хранилища тредов v2 (прокси workflow ↔ activities воркера) через
настоящий конвертер Temporal: ядро видит то же хранилище, что локально, а
история растёт разницами, а не полным состоянием."""

from copy import deepcopy

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, SessionState, ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings
from gc_testkit.thread_store_conformance import assert_conforms_async
from gc_core_temporalio import thread_store_activities
from gc_core_temporalio.thread_store_activities import ThreadStoreActivities
from gc_core_temporalio.thread_store_contracts import (
    PAGE_BYTES,
    ThreadCall,
    ThreadReply,
)
from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
from gc_core_temporalio.thread_store_transport import _STORE_ERRORS
from temporalio.activity import _Definition
from temporalio.converter import DataConverter
from temporalio.exceptions import ApplicationError

KEY = ThreadKey(system="cli", thread="t")


async def wire(value, result_type):
    converter = DataConverter.default
    payloads = await converter.encode([value])
    return (await converter.decode(payloads, [result_type]))[0]


class Wire:
    """Транспорт прокси: вызов activity через конвертер, учёт вызовов."""

    def __init__(self, store):
        self.activities = ThreadStoreActivities({"main": store})
        self.methods = {
            _Definition.must_from_callable(method).name: method
            for method in self.activities.registered
        }
        self.calls: list[tuple[str, ThreadCall]] = []
        self.replies: list[tuple[str, ThreadReply]] = []
        #: Имя операции → сколько ещё раз её «повторить после записи».
        self.lose_reply: dict[str, int] = {}

    async def __call__(self, name, call):
        decoded = await wire(call, ThreadCall)
        self.calls.append((name.rsplit(".", 1)[-1], deepcopy(decoded)))
        try:
            result = await self.methods[name](decoded)
            if self.lose_reply.get(name.rsplit(".", 1)[-1]):
                # Воркер записал и упал до ответа: Temporal повторит activity.
                self.lose_reply[name.rsplit(".", 1)[-1]] -= 1
                result = await self.methods[name](decoded)
        except ApplicationError as exc:
            raise _STORE_ERRORS[exc.type](exc.message) from exc
        reply = await wire(result, ThreadReply)
        self.replies.append((name.rsplit(".", 1)[-1], reply))
        return reply

    def ops(self) -> list[str]:
        return [op for op, _call in self.calls]


def proxy_for(store, owner="wf"):
    transport = Wire(store)
    return TemporalThreadStore("main", transport, owner=owner), transport


def test_session_port_over_the_wire_keeps_old_contract():
    def factory():
        return proxy_for(MemoryThreadStore())[0].port

    assert len(assert_conforms_async(factory)) == 18


async def _session(proxy):
    return await ThreadSession.afrom_config(
        settings=make_settings({}),
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=proxy,
    )


def _message(n, content=None):
    return {"id": f"m{n}", "role": "user", "content": content or f"step {n}"}


async def test_a_step_is_one_commit_activity():
    """Аренда взята в начале сессии: шаг — одна activity ``commit``."""
    store = MemoryThreadStore()
    proxy, transport = proxy_for(store)
    await proxy.claim(KEY, LeaseRequest(owner="wf"))
    session = await _session(proxy)
    transport.calls.clear()
    for n in range(3):
        await session.apersist_batch([_message(n)], previous_response_id=None)
    # Сессия перечитывает meta перед шагом (сливает чужие секции): после
    # своей записи прокси знает запись треда, и ответ — «не изменилась».
    assert transport.ops() == ["get", "commit"] * 3
    gets = [reply for op, reply in transport.replies if op == "get"]
    assert all(reply.unchanged for reply in gets[1:]), gets
    assert [
        e.data["id"] for e in await store.read_stream(KEY, "messages")
    ] == [
        "m0",
        "m1",
        "m2",
    ]


async def test_state_goes_as_delta_and_full_after_lost_reply():
    """Состояние — разницей к известному; повтор activity после записи (ответ
    потерян) воркер не соберёт из разницы — прокси шлёт целиком, хранилище
    узнаёт операцию по ``op_id``: та же квитанция, без дубля."""
    from gc_core.threads.models import ThreadChange

    store = MemoryThreadStore()
    proxy, transport = proxy_for(store)
    lease = (await proxy.claim(KEY, LeaseRequest(owner="wf"))).lease
    big = "x" * 50_000
    state = SessionState(working_memory={"k": big})
    first = await proxy.commit(
        KEY,
        ThreadChange(
            op_id="op1",
            expected_revision=0,
            lease=lease,
            append_messages=[_message(0)],
            set_state=state,
        ),
    )
    transport.calls.clear()
    transport.lose_reply["commit"] = 1
    await proxy.commit(
        KEY,
        ThreadChange(
            op_id="op2",
            expected_revision=first.revision,
            lease=lease,
            append_messages=[_message(1)],
            set_state=state.model_copy(update={"notes": "заметка"}),
        ),
    )
    sent = [call.args for op, call in transport.calls if op == "commit"]
    assert "delta" in sent[0]["state"], "первая попытка — разницей"
    assert len(str(sent[0]["state"])) < len(big)
    assert "full" in sent[1]["state"], "после потерянного ответа — целиком"
    ids = [e.data["id"] for e in await store.read_stream(KEY, "messages")]
    assert ids == ["m0", "m1"]
    record = await store.get_thread(KEY)
    assert record.state.notes == "заметка"
    assert record.state.working_memory == {"k": big}


async def test_long_history_is_read_in_chunks():
    store = MemoryThreadStore()
    lease = (await store.claim(KEY, LeaseRequest(owner="wf"))).lease
    from gc_core.threads.models import ThreadChange

    await store.commit(
        KEY,
        ThreadChange(
            op_id="op",
            expected_revision=0,
            lease=lease,
            append_messages=[_message(n, "ж" * 30_000) for n in range(40)],
        ),
    )
    proxy, transport = proxy_for(store)
    entries = await proxy.read_stream(KEY, "messages")
    assert [e.data["id"] for e in entries] == [f"m{n}" for n in range(40)]
    reads = [call for op, call in transport.calls if op == "read_stream"]
    assert len(reads) > 1, "история больше страницы — несколько activities"
    replies = [len(str(call.args)) for call in reads]
    assert max(replies) < PAGE_BYTES


async def test_known_record_comes_as_delta_or_unchanged():
    store = MemoryThreadStore()
    proxy, transport = proxy_for(store)
    await proxy.claim(KEY, LeaseRequest(owner="wf"))
    session = await _session(proxy)
    await session.apersist_batch([_message(0)], previous_response_id=None)
    first = await proxy.get_thread(KEY)
    again = await proxy.get_thread(KEY)
    assert again == first
    await session.apersist_batch([_message(1)], previous_response_id=None)
    newer = await proxy.get_thread(KEY)
    assert newer.revision == first.revision + 1
    # Чужая запись рядом с ходом (статус от фронтенда) — разница к известной.
    from gc_core.threads.models import SideChange

    await store.append_side(
        KEY, SideChange(op_id="tui", patch_frontend=("tui", {"title": "t"}))
    )
    assert (await proxy.get_thread(KEY)).frontend["tui"] == {"title": "t"}
    replies = [reply for op, reply in transport.replies if op == "get"]
    assert any(r.unchanged for r in replies), "без изменений — признак"
    assert replies[-1].delta is not None, "после чужой записи — разница"
    assert replies[-1].chunk is None


async def test_fetch_cache_stays_on_the_worker():
    """Кэш ``fetch_url`` хранилища (его вёл локальный режим) не идёт в
    историю и не стирается записью workflow с пустым кэшем."""
    store = MemoryThreadStore()
    lease = (await store.claim(KEY, LeaseRequest(owner="wf"))).lease
    from gc_core.threads.models import ThreadChange

    cache = {"https://example.org": {"text": "страница" * 1000}}
    await store.commit(
        KEY,
        ThreadChange(
            op_id="op",
            expected_revision=0,
            lease=lease,
            set_state=SessionState(fetch_url_cache=cache),
        ),
    )
    proxy, transport = proxy_for(store)
    proxy.adopt(KEY, lease)
    session = await _session(proxy)
    assert session.meta.get("shared", {}).get("fetch_url_cache") in (None, {})
    await session.apersist_batch([_message(0)], previous_response_id=None)
    assert "страница" not in str([call.args for _op, call in transport.calls])
    assert (await store.get_thread(KEY)).state.fetch_url_cache == cache


async def test_history_keys_of_the_file_store_cross_the_wire(tmp_path):
    """Дедуп сессии под Temporal — по ключам файлового хранилища, как
    локально (у старых записей без id ключи есть и до синтеза id)."""
    store = FileThreadStore(tmp_path)
    proxy, _transport = proxy_for(store)
    await proxy.claim(KEY, LeaseRequest(owner="wf"))
    session = await _session(proxy)
    await session.apersist_batch([_message(0)], previous_response_id=None)
    await proxy.read_stream(KEY, "messages")
    assert proxy.history_keys(KEY) == store.history_keys(KEY)


async def test_claim_takes_the_thread_from_a_dead_owner(monkeypatch):
    store = MemoryThreadStore()
    await store.claim(KEY, LeaseRequest(owner="dead-wf"))
    proxy, _transport = proxy_for(store, owner="new-wf")

    async def alive(owner, client=None):
        return owner != "dead-wf"

    monkeypatch.setattr(thread_store_activities, "_owner_alive", alive)
    record = await proxy.claim(KEY, LeaseRequest(owner="new-wf"))
    assert record.lease.owner == "new-wf"
    assert record.lease.epoch == 2, "смена владельца — новая эпоха"


async def test_claim_never_takes_from_a_live_owner(monkeypatch):
    store = MemoryThreadStore()
    await store.claim(KEY, LeaseRequest(owner="live-wf"))
    proxy, _transport = proxy_for(store, owner="new-wf")

    async def alive(owner, client=None):
        return True

    monkeypatch.setattr(thread_store_activities, "_owner_alive", alive)
    with pytest.raises(OwnershipError):
        await proxy.claim(KEY, LeaseRequest(owner="new-wf"))
