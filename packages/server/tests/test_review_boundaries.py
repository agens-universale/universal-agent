"""Гонки приёма задач и границы завершения: регрессии ревью."""

import asyncio
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.environment import utc_now
from gc_core.threads import runtime_rules
from gc_core.threads.facade import Threads
from gc_core.threads.models import SideChange, ThreadKey
from gc_server.thread_ownership import claim_surface
from gc_server.webhook import recovery, status
from gc_server.webhook.surface import WebhookSurface
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="test", thread="t")


async def test_webhook_and_swarm_elect_one_surface(monkeypatch):
    threads = Threads(MemoryThreadStore())
    original = threads.get
    reads = 0
    both = asyncio.Event()

    async def snapshot(key):
        nonlocal reads
        record = await original(key)
        reads += 1
        if reads <= 2:
            if reads == 2:
                both.set()
            await both.wait()
        return record

    monkeypatch.setattr(threads, "get", snapshot)
    results = await asyncio.gather(
        claim_surface(threads, KEY, "webhook"),
        claim_surface(threads, KEY, "swarm"),
        return_exceptions=True,
    )
    assert sum(r is None for r in results) == 1
    assert (
        sum(
            isinstance(r, HTTPException) and r.status_code == 409
            for r in results
        )
        == 1
    )
    assert len((await original(KEY)).frontend) == 1


@pytest.mark.parametrize("ending", ["cancelled", "failed"])
async def test_finishing_does_not_abandon_a_new_message(ending):
    threads = Threads(MemoryThreadStore())
    await status.asubmit(threads, KEY, {"content": "old"})
    await status.aset_status(
        threads,
        KEY,
        status="running",
        run_id="r",
        inbox_started_upto=1,
        inbox_first=1,
    )
    if ending == "cancelled":
        await threads.request_cancel(KEY)
    await status.asubmit(threads, KEY, {"content": "new"})
    run = SimpleNamespace(
        ending=ending,
        result=None,
        error=None,
        system=KEY.system,
        task_id=KEY.thread,
        notes={"run_id": "r"},
    )
    surface = SimpleNamespace(
        threads=lambda: threads, notify=lambda *args: None
    )
    await WebhookSurface._amark_finished(surface, run)
    record = await threads.get(KEY)
    assert status.status_of(record)["inbox_upto"] == 1
    assert recovery._orphaned(record, utc_now())


def test_late_cancel_does_not_stop_a_run_after_its_boundary():
    stopped = []
    executor = SimpleNamespace(
        _control_seen=0,
        _cancel_target=None,
        _inbox_first=2,
        cancelled_inbox_upto=None,
        stop_run=lambda: stopped.append(True),
    )
    control = lambda seq, upto: SimpleNamespace(
        seq=seq, data={"type": "cancel", "upto_inbox_seq": upto}
    )
    AgentExecutor._apply_control(executor, [control(1, 1)])
    assert stopped == []
    AgentExecutor._apply_control(executor, [control(2, 2)])
    assert stopped == [True]


async def test_third_recovery_attempt_is_not_exhausted_while_it_is_building():
    threads = Threads(MemoryThreadStore())
    await status.asubmit(threads, KEY, {"content": "work"})
    record = await threads.get(KEY)
    await threads.store.append_side(
        KEY,
        SideChange(
            op_id="running",
            set_runtime=runtime_rules.with_turn(record.runtime, running=True),
        ),
    )
    await status.aset_status(
        threads, KEY, status="running", recovery_attempts=2
    )
    record = await threads.get(KEY)
    assert await recovery._aclaim(
        threads, record, status.status_of(record), 2, False
    )
    events = []

    async def start(*_args, prepare):
        return await prepare()

    surface = SimpleNamespace(
        astart_recovered=start, notify=lambda *args: events.append(args)
    )
    assert not await recovery._acontinue(
        surface, threads, await threads.get(KEY)
    )
    final = status.status_of(await threads.get(KEY))
    assert final["status"] == "running" and final["recovery_attempts"] == 3
    assert events == []
