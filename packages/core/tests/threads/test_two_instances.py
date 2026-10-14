"""Два инстанса на одном каталоге тредов (спека хранилищ v2, 9.6): живую
аренду не перехватить, истёкшую — можно, а старый владелец после перехвата
не испортит историю нового (фенсинг)."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_port import SessionPort
from gc_testkit.thread_store_conformance_v2 import FakeClock

KEY = ThreadKey(system="srv", thread="t1")


def _msg(i):
    return {"id": f"m{i}", "role": "user", "content": f"текст {i}"}


def test_second_instance_waits_for_lease_and_fences_the_first(tmp_path):
    clock = FakeClock()
    a = SessionPort(FileThreadStore(tmp_path, clock=clock), owner="host-a")
    b = SessionPort(FileThreadStore(tmp_path, clock=clock), owner="host-b")

    async def scenario():
        await a.aappend_messages("srv", "t1", [_msg(1)], known_keys=set())
        with pytest.raises(OwnershipError):
            await b.aappend_messages("srv", "t1", [_msg(2)], known_keys=set())

        clock.advance(timedelta(seconds=61))  # host-a пропал
        await b.aappend_messages("srv", "t1", [_msg(3)], known_keys=set())

        # host-a «ожил» со старой арендой: запись отвергнута, история цела.
        with pytest.raises(OwnershipError):
            await a.aappend_messages("srv", "t1", [_msg(4)], known_keys=set())
        state = await b.aload_thread_state("srv", "t1")
        return [m["id"] for m in state.messages]

    assert asyncio.run(scenario()) == ["m1", "m3"]


def test_same_machine_processes_do_not_block_each_other(tmp_path):
    clock = FakeClock()
    first = SessionPort(FileThreadStore(tmp_path, clock=clock), owner="host")
    second = SessionPort(FileThreadStore(tmp_path, clock=clock), owner="host")

    async def scenario():
        await first.aappend_messages("srv", "t1", [_msg(1)], known_keys=set())
        await second.aappend_messages("srv", "t1", [_msg(2)], known_keys=set())
        await first.aappend_messages("srv", "t1", [_msg(3)], known_keys=set())
        state = await first.aload_thread_state("srv", "t1")
        return [m["id"] for m in state.messages]

    # CLI и TUI одной машины пишут в тред по очереди, как и раньше.
    assert asyncio.run(scenario()) == ["m1", "m2", "m3"]
