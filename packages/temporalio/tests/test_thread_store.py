"""Провод хранилища тредов v2 (прокси workflow ↔ activities воркера) через
настоящий конвертер Temporal: ядро видит то же хранилище, что локально, а
история растёт разницами, а не полным состоянием."""

from copy import deepcopy
from datetime import timedelta

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, SessionState, ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings
from gc_testkit.session_checks import CHECKS as SESSION_CHECKS
from gc_testkit.session_checks import aassert_session_works
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
        self.store = store
        self._start_worker()
        self.calls: list[tuple[str, ThreadCall]] = []
        self.replies: list[tuple[str, ThreadReply]] = []
        #: Имя операции → сколько ещё раз её «повторить после записи».
        self.lose_reply: dict[str, int] = {}
        #: Повтор после потерянного ответа — на новом воркере (без кэша).
        self.restart_on_retry = False

    def _start_worker(self):
        self.activities = ThreadStoreActivities({"main": self.store})
        self.methods = {
            _Definition.must_from_callable(method).name: method
            for method in self.activities.registered
        }

    async def __call__(self, name, call):
        decoded = await wire(call, ThreadCall)
        self.calls.append((name.rsplit(".", 1)[-1], deepcopy(decoded)))
        try:
            result = await self.methods[name](decoded)
            if self.lose_reply.get(name.rsplit(".", 1)[-1]):
                # Воркер записал и упал до ответа: Temporal повторит activity.
                self.lose_reply[name.rsplit(".", 1)[-1]] -= 1
                if self.restart_on_retry:
                    self._start_worker()
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


async def test_session_over_the_wire_works():
    """Набор пути хода — сессия агента поверх прокси workflow."""
    passed = await aassert_session_works(
        lambda: proxy_for(MemoryThreadStore())[0]
    )
    assert passed == [check.__name__ for check in SESSION_CHECKS]


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
    # Сессия пишет из своего снимка частей: перед шагом тред не читается.
    assert transport.ops() == ["commit"] * 3
    assert [
        e.data["id"] for e in await store.read_stream(KEY, "messages")
    ] == [
        "m0",
        "m1",
        "m2",
    ]


@pytest.mark.parametrize("restart", [False, True])
async def test_state_goes_as_delta_and_retry_after_lost_reply_writes_once(
    restart,
):
    """Состояние — разницей к известной записи. Повтор activity после записи
    (ответ потерян): тот же воркер собирает ту же запись из кэша; новый
    воркер базы не знает — прокси шлёт части с нуля. Хранилище узнаёт
    операцию по ``op_id``: та же квитанция, история без дубля."""
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
    await proxy.get_thread(KEY)
    transport.calls.clear()
    transport.lose_reply["commit"] = 1
    transport.restart_on_retry = restart
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
    first_state = sent[0]["parts"]["set_state"]
    assert first_state.get("known"), "первая попытка — разницей к известной"
    assert len(str(first_state)) < len(big)
    if restart:
        retry_state = sent[1]["parts"]["set_state"]
        assert not retry_state.get("known"), "новый воркер — части с нуля"
    else:
        assert len(sent) == 1, "тот же воркер собрал повтор из кэша"
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
    assert not session.state.fetch_url_cache
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


class _Session:
    """Запись, как её шлёт сессия на шаге: сообщения и все части meta."""

    def __init__(self, proxy):
        self.proxy = proxy
        self.revision = 0

    async def step(self, n, *, runtime=None, state=None, text=None):
        from gc_core.threads.models import (
            RuntimeStatus,
            ThreadChange,
            ThreadInfo,
            Usage,
        )

        lease = self.proxy.lease
        receipt = await self.proxy.commit(
            KEY,
            ThreadChange(
                op_id=f"step{n}",
                expected_revision=self.revision,
                lease=lease,
                append_messages=[_message(n, text)],
                set_state=state or SessionState(notes=f"шаг {n}"),
                set_usage=Usage(by_category={"main": {"calls": n}}),
                set_info=ThreadInfo(title="t"),
                set_runtime=runtime or RuntimeStatus(turn="running"),
            ),
        )
        self.revision = receipt.revision


async def _stepping(store, transport_factory=None):
    proxy, transport = proxy_for(store)
    await proxy.claim(KEY, LeaseRequest(owner="wf"))
    await proxy.get_thread(KEY)
    return proxy, transport, _Session(proxy)


async def test_big_runtime_goes_as_delta_every_step():
    """Снимок MCP-тулов в ``runtime`` (десятки КБ) не уходит в историю на
    каждом шаге: части записи — разницей к известной записи."""
    from gc_core.threads.models import RuntimeStatus

    store = MemoryThreadStore()
    proxy, transport, session = await _stepping(store)
    runtime = RuntimeStatus(turn="running", mcp_tools={"tools": "т" * 30_000})
    await session.step(0, runtime=runtime)
    transport.calls.clear()
    for n in range(1, 4):
        await proxy.get_thread(KEY)
        await session.step(n, runtime=runtime)
    sizes = [len(str(call.args)) for _op, call in transport.calls]
    assert max(sizes) < 5_000, sizes
    assert (await store.get_thread(KEY)).runtime.mcp_tools == runtime.mcp_tools


async def test_user_text_in_state_goes_by_reference():
    """Текст запроса и в сообщении, и в журнале намерений — в записи один
    раз: в состоянии он идёт ссылкой на сообщение."""
    store = MemoryThreadStore()
    proxy, transport, session = await _stepping(store)
    text = "ж" * 100_000
    state = SessionState(current_intent=[{"text": text}])
    await session.step(0, state=state, text=text)
    [(_op, call)] = [c for c in transport.calls if c[0] == "commit"]
    assert len(str(call.args)) < 1.3 * len(str(text.encode("unicode_escape")))
    assert (await store.get_thread(KEY)).state.current_intent == [
        {"text": text}
    ]


async def test_worker_without_cache_takes_the_known_record_from_store():
    """Запись на воркере, который не видел записи workflow (другой воркер,
    вытеснение из кэша): база — запись хранилища с тем же отпечатком, после
    записи следующий ``get`` — «не изменилась», а не запись целиком."""
    store = MemoryThreadStore()
    proxy, transport, session = await _stepping(store)

    async def fresh_worker(name, call):
        transport.activities = ThreadStoreActivities({"main": store})
        transport.methods = {
            _Definition.must_from_callable(m).name: m
            for m in transport.activities.registered
        }
        return await Wire.__call__(transport, name, call)

    proxy._call = fresh_worker
    for n in range(3):
        await proxy.get_thread(KEY)
        await session.step(n)
    gets = [reply for op, reply in transport.replies if op == "get"]
    assert all(reply.unchanged for reply in gets[1:]), gets


async def test_thread_changing_on_every_read_is_a_domain_failure():
    """Запись меняется между кусками на каждом чтении — отказ контракта
    хранилища (доменный: ход падает, workflow task — нет)."""
    from gc_core.threads.errors import ThreadStoreError
    from gc_core_temporalio.thread_store_contracts import ThreadChanged

    assert issubclass(ThreadChanged, ThreadStoreError)


async def test_timed_lease_of_a_local_process_is_not_taken_over(monkeypatch):
    """Аренда со сроком (TUI/CLI на машине) истечёт сама: «нет такого
    workflow» не значит «владелец мёртв»."""
    store = MemoryThreadStore()
    await store.claim(KEY, LeaseRequest(owner="my-laptop", ttl=timedelta(60)))
    proxy, _transport = proxy_for(store, owner="wf#run")

    async def gone(owner, client=None):
        return False

    monkeypatch.setattr(thread_store_activities, "_owner_alive", gone)
    with pytest.raises(OwnershipError):
        await proxy.claim(KEY, LeaseRequest(owner="wf#run"))
    released = await thread_store_activities.release_stale_owner(
        store, KEY.system, KEY.thread, client=None
    )
    assert released is None
    assert (await store.get_thread(KEY)).lease.owner == "my-laptop"


class _Described:
    def __init__(self, first_run_id, running=True):
        from temporalio.client import WorkflowExecutionStatus

        self.status = (
            WorkflowExecutionStatus.RUNNING
            if running
            else WorkflowExecutionStatus.TERMINATED
        )
        self.raw_info = type("Raw", (), {"first_run_id": first_run_id})()


class _Client:
    def __init__(self, first_run_id):
        self.first_run_id = first_run_id

    def get_workflow_handle(self, _workflow_id):
        client = self

        class Handle:
            async def describe(self):
                return _Described(client.first_run_id)

        return Handle()


async def test_owner_is_the_run_chain_not_the_workflow_id():
    """Новый запуск с тем же workflow ID (после terminate) — другая цепочка:
    владелец прежней цепочки мёртв, его запись отсекает новая эпоха."""
    alive = thread_store_activities._owner_alive
    assert await alive("session#chain-1", _Client("chain-1"))
    assert not await alive("session#chain-1", _Client("chain-2"))
