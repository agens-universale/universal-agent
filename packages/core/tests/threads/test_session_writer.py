"""Запись сессии треда: аренда без перезахвата, ревизия из квитанции,
повтор временного сбоя с тем же ``op_id``."""

from datetime import timedelta

import pytest
from gc_core.threads.errors import (
    OperationMismatch,
    StoreUnavailable,
    ThreadBusy,
)
from gc_core.threads.models import SessionState, ThreadKey
from gc_core.threads.session_writer import RetryPolicy, SessionWriter
from gc_core.utils import aretry
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.thread_store_conformance_v2 import FakeClock

KEY = ThreadKey(system="cli", thread="w")
TTL = timedelta(seconds=60)
FAST = RetryPolicy(max_attempts=5, initial_delay_sec=0, backoff_factor=1)


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    pauses: list = []

    async def fake_sleep(seconds):
        pauses.append(seconds)

    monkeypatch.setattr(aretry.asyncio, "sleep", fake_sleep)
    return pauses


def _writer(store, owner, clock, **kw):
    return SessionWriter(
        store, KEY, owner=owner, ttl=TTL, retry=FAST, clock=clock, **kw
    )


async def test_lost_lease_is_busy_and_does_not_reclaim():
    """Сценарий ревью №1: A завис, B взял тред и записал свой ход — запись A
    отвергается, состояние B цело, тред остаётся за B."""
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    a = _writer(store, "A", clock)
    await a.aclaim()
    clock.advance(TTL * 2)
    b = _writer(store, "B", clock)
    await b.aclaim()
    await b.acommit(set_state=SessionState(inbox_seq=5, notes="B"))
    with pytest.raises(ThreadBusy) as busy:
        await a.acommit(set_state=SessionState(inbox_seq=1, notes="A"))
    assert busy.value.owner == "B"
    record = await store.get_thread(KEY)
    assert record.state.inbox_seq == 5 and record.state.notes == "B"
    assert record.lease.owner == "B"
    with pytest.raises(ThreadBusy):
        await a.acommit(set_state=SessionState(notes="A ещё раз"))
    assert (await store.get_thread(KEY)).state.notes == "B"


async def test_conflict_under_own_lease_is_busy_not_overwrite():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    a = _writer(store, "A", clock)
    await a.aclaim()
    a.revision += 7  # чужая запись, о которой A не знает
    with pytest.raises(ThreadBusy):
        await a.acommit(set_state=SessionState(notes="A"))
    assert (await store.get_thread(KEY)).state.notes == ""


async def test_transient_error_retries_with_same_op_id(no_sleep):
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()
    real = store.commit
    seen: list = []

    async def flaky(key, change):
        seen.append(change.op_id)
        if len(seen) < 3:
            raise StoreUnavailable("связь потеряна")
        return await real(key, change)

    store.commit = flaky
    msg = {"id": "m1", "role": "user", "content": "x"}
    await w.acommit(append_messages=[msg])
    assert len(seen) == 3 and len(set(seen)) == 1
    assert [
        e.data["id"] for e in await store.read_stream(KEY, "messages")
    ] == ["m1"]
    assert len(no_sleep) == 2


async def test_contract_errors_are_not_retried():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()
    calls: list = []

    async def mismatch(key, change):
        calls.append(change)
        raise OperationMismatch("другое содержимое")

    store.commit = mismatch
    with pytest.raises(OperationMismatch):
        await w.acommit(set_state=SessionState())
    assert len(calls) == 1


async def test_retry_gives_up_after_max_attempts():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()
    calls: list = []

    async def down(key, change):
        calls.append(change)
        raise StoreUnavailable("недоступно")

    store.commit = down
    with pytest.raises(StoreUnavailable):
        await w.acommit(set_state=SessionState())
    assert len(calls) == 5


async def test_infinite_lease_not_renewed_or_released():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = SessionWriter(
        store, KEY, owner="wf", ttl=None, retry=FAST, clock=clock
    )
    await w.aclaim()
    claims: list = []
    real = store.claim

    async def counted(key, request):
        claims.append(request)
        return await real(key, request)

    store.claim = counted
    clock.advance(timedelta(days=3))
    await w.acommit(set_state=SessionState(notes="x"))
    await w.arenew()
    await w.arelease()
    assert claims == []
    assert (await store.get_thread(KEY)).lease.owner == "wf"


async def test_lease_renewed_when_half_left():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()
    first = w.lease.expires_at
    clock.advance(TTL * 0.6)
    await w.acommit(set_state=SessionState(notes="x"))
    assert w.lease.expires_at > first


async def test_first_write_claims_lazily_but_never_after_loss():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.acommit(set_state=SessionState(notes="лениво"))
    assert (await store.get_thread(KEY)).lease.owner == "A"
    clock.advance(TTL * 2)
    await _writer(store, "B", clock).aclaim()
    with pytest.raises(ThreadBusy):
        await w.acommit(set_state=SessionState(notes="после потери"))
    clock.advance(TTL * 2)  # аренда B истекла — и всё равно без перезахвата
    with pytest.raises(ThreadBusy):
        await w.acommit(set_state=SessionState(notes="снова"))


async def test_release_frees_thread():
    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()
    await w.arelease()
    assert (await store.get_thread(KEY)).lease is None
    await _writer(store, "B", clock).aclaim()


async def test_op_ids_come_from_the_execution_environment():
    """В workflow ``op_id`` — из детерминированного генератора: replay и
    повтор activity дают те же операции."""
    from datetime import UTC, datetime
    from itertools import count

    from gc_core.execution.environment import execution_environment

    runs = []
    for _ in range(2):
        seq = count()
        store = MemoryThreadStore()
        seen: list = []
        real = store.commit

        async def recording(key, change, real=real, seen=seen):
            seen.append(change.op_id)
            return await real(key, change)

        store.commit = recording
        with execution_environment(
            new_id=lambda prefix, seq=seq: f"{prefix}-{next(seq)}",
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            w = SessionWriter(store, KEY, owner="wf", ttl=None, retry=FAST)
            await w.aclaim()
            for n in range(2):
                await w.acommit(
                    append_messages=[
                        {"id": f"m{n}", "role": "user", "content": str(n)}
                    ]
                )
        runs.append(seen)
    assert runs[0] == runs[1] and len(runs[0]) == 2
    assert all(op.startswith("session-") for op in runs[0])


async def test_adopted_lease_needs_no_claim():
    """Аренда, взятая workflow раньше (до Continue-As-New, у родителя
    субагента): запись идёт по ней без нового ``claim``."""
    from gc_core.threads.models import LeaseRequest

    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    record = await store.claim(KEY, LeaseRequest(owner="wf", ttl=None))
    claims: list = []
    real = store.claim

    async def counted(key, request):
        claims.append(request)
        return await real(key, request)

    store.claim = counted
    w = SessionWriter(
        store, KEY, owner="wf", ttl=None, retry=FAST, lease=record.lease
    )
    await w.aclaim()
    w.revision = record.revision
    await w.acommit(set_state=SessionState(notes="x"))
    assert claims == []


async def test_busy_refusal_names_the_owner_and_logs_no_error(caplog):
    """Чужая живая аренда — ожидаемый отказ: текст для человека («занят»,
    чей тред), без ERROR в логе — это не сбой хранилища."""
    import logging

    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    await _writer(store, "tui/1/abcd1234", clock).aclaim()
    with caplog.at_level(logging.DEBUG), pytest.raises(ThreadBusy) as busy:
        await _writer(store, "cli", clock).aclaim()
    assert "занят" in str(busy.value) and "tui/1/abcd1234" in str(busy.value)
    assert not [r for r in caplog.records if r.levelno >= logging.ERROR]


async def test_exhausted_retries_are_logged_as_error(caplog):
    import logging

    clock = FakeClock()
    store = MemoryThreadStore(clock=clock)
    w = _writer(store, "A", clock)
    await w.aclaim()

    async def down(key, change):
        raise StoreUnavailable("недоступно")

    store.commit = down
    with caplog.at_level(logging.DEBUG), pytest.raises(StoreUnavailable):
        await w.acommit(set_state=SessionState())
    errors = [r for r in caplog.records if r.levelno >= logging.ERROR]
    assert len(errors) == 1 and "thread commit" in errors[0].getMessage()
