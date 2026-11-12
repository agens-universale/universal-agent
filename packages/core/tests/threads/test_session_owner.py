"""Владелец аренды — сессия, а не машина (спека сервера, 13.1).

Два хода одного процесса на одном треде больше не «свои» друг другу: второй
получает ``ThreadBusy`` до любой работы. Записи фасада отпускают аренду сразу —
иначе следующий ход ждал бы срок аренды.
"""

import os

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.threads.errors import OwnershipError, ThreadBusy
from gc_core.threads.facade import Threads
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.instance import instance_id, session_owner
from gc_core.threads.models import ThreadKey
from gc_core.threads.session_writer import SessionWriter
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="cli", thread="t1")


def _msg(i):
    return {"id": f"m{i}", "role": "user", "content": f"текст {i}"}


def test_session_owner_names_instance_process_and_session():
    first, second = session_owner(), session_owner()
    instance, pid, tail = first.rsplit("/", 2)
    assert instance == instance_id()
    assert pid == str(os.getpid())
    assert len(tail) == 8
    assert first != second


async def _session(tmp_path, store):
    return await ThreadSession.afrom_config(
        settings=make_settings({"paths.threads_data_path": str(tmp_path)}),
        system_id=KEY.system,
        thread_id=KEY.thread,
        store=store,
    )


async def test_each_session_owns_its_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    first = await _session(tmp_path, store)
    second = await _session(tmp_path, store)
    assert first.writer.owner != second.writer.owner
    await first.writer.aclaim()
    with pytest.raises(ThreadBusy) as busy:
        await second.writer.aclaim()
    assert busy.value.owner == first.writer.owner
    assert isinstance(busy.value, OwnershipError)


async def test_released_lease_lets_the_next_session_in(tmp_path):
    store = FileThreadStore(tmp_path)
    first = await _session(tmp_path, store)
    await first.writer.aclaim()
    await first.arelease_lease()
    second = await _session(tmp_path, store)
    await second.writer.aclaim()


async def test_store_lease_policy_is_used(tmp_path):
    """Temporal задаёт политику аренды хранилищем (бессрочная аренда
    workflow) — сессия пишет по ней, а не своей сессией."""
    store = FileThreadStore(tmp_path)
    store.session_lease_policy = lambda: ("workflow-1", None, None)
    session = await _session(tmp_path, store)
    assert session.writer.owner == "workflow-1"
    assert session.writer.ttl is None


def _runner(store):
    return SessionWriter(store, KEY, owner="runner")


async def test_facade_write_releases_the_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    await Threads(store).append_messages(KEY, [_msg(1)])
    # Сразу после записи тред свободен: ход другой сессии его берёт.
    await SessionWriter(store, KEY, owner="next").aclaim()


async def test_facade_discard_releases_the_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    threads = Threads(store)
    await threads.append_messages(KEY, [_msg(1), _msg(2)])
    assert await threads.discard_message(KEY, "m1")
    await SessionWriter(store, KEY, owner="next").aclaim()


async def test_facade_write_into_a_busy_thread_is_thread_busy(tmp_path):
    store = FileThreadStore(tmp_path)
    await _runner(store).aclaim()
    with pytest.raises(ThreadBusy):
        await Threads(store).append_messages(KEY, [_msg(1)])


async def test_busy_owner_names_a_live_foreign_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    threads = Threads(store)
    assert await threads.busy_owner(KEY) is None
    runner = _runner(store)
    await runner.aclaim()
    assert await threads.busy_owner(KEY) == "runner"
    await runner.arelease()
    assert await threads.busy_owner(KEY) is None
