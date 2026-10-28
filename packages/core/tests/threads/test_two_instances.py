"""Два инстанса на одном каталоге тредов (спека хранилищ v2, 9.6): живую
аренду не перехватить, истёкшую — можно, а старый владелец после перехвата
не испортит историю нового (фенсинг)."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.threads.errors import OwnershipError
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_writer import SessionWriter
from gc_testkit.thread_store_conformance_v2 import FakeClock

KEY = ThreadKey(system="srv", thread="t1")


def _msg(i):
    return {"id": f"m{i}", "role": "user", "content": f"текст {i}"}


def _writer(tmp_path, clock, owner):
    store = FileThreadStore(tmp_path, clock=clock)
    return SessionWriter(store, KEY, owner=owner, clock=clock)


def test_second_instance_waits_for_lease_and_fences_the_first(tmp_path):
    clock = FakeClock()
    a = _writer(tmp_path, clock, "host-a")
    b = _writer(tmp_path, clock, "host-b")

    async def scenario():
        await a.acommit(append_messages=[_msg(1)])
        with pytest.raises(OwnershipError):
            await b.acommit(append_messages=[_msg(2)])

        clock.advance(timedelta(seconds=61))  # host-a пропал
        b2 = _writer(tmp_path, clock, "host-b")
        await b2.acommit(append_messages=[_msg(3)])

        # host-a «ожил» со старой арендой: запись отвергнута, история цела.
        with pytest.raises(OwnershipError):
            await a.acommit(append_messages=[_msg(4)])
        entries = await b2.store.read_stream(KEY, "messages")
        return [e.data["id"] for e in entries]

    assert asyncio.run(scenario()) == ["m1", "m3"]


def test_same_machine_processes_take_turns(tmp_path):
    """CLI и TUI одной машины пишут в тред по очереди: запись фасада
    отпускает аренду сразу."""
    clock = FakeClock()

    async def scenario():
        for i in (1, 2, 3):
            threads = Threads(FileThreadStore(tmp_path, clock=clock))
            await threads.append_messages(KEY, [_msg(i)])
        entries = await FileThreadStore(tmp_path, clock=clock).read_stream(
            KEY, "messages"
        )
        return [e.data["id"] for e in entries]

    assert asyncio.run(scenario()) == ["m1", "m2", "m3"]
