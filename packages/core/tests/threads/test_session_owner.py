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
from gc_core.threads.session_port import SessionPort, attach_session_port
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
    assert first.port is not second.port
    await first.port.aclaim(KEY.system, KEY.thread)
    with pytest.raises(ThreadBusy) as busy:
        await second.port.aclaim(KEY.system, KEY.thread)
    assert busy.value.owner == first.port.owner
    assert isinstance(busy.value, OwnershipError)


async def test_released_lease_lets_the_next_session_in(tmp_path):
    store = FileThreadStore(tmp_path)
    first = await _session(tmp_path, store)
    await first.port.aclaim(KEY.system, KEY.thread)
    await first.port.arelease_lease(KEY.system, KEY.thread)
    second = await _session(tmp_path, store)
    await second.port.aclaim(KEY.system, KEY.thread)


async def test_attached_port_is_kept(tmp_path):
    """Temporal прикрепляет к хранилищу свой порт (бессрочная аренда
    workflow) — сессия пишет через него, а не через свой."""
    store = FileThreadStore(tmp_path)
    durable = SessionPort(store, owner="workflow-1", ttl=None)
    attach_session_port(store, durable)
    session = await _session(tmp_path, store)
    assert session.port is durable


async def test_facade_write_releases_the_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    await Threads(store).append_messages(KEY, [_msg(1)])
    # Сразу после записи тред свободен: ход другой сессии его берёт.
    await SessionPort(store, owner="next").aclaim(KEY.system, KEY.thread)


async def test_facade_discard_releases_the_lease(tmp_path):
    store = FileThreadStore(tmp_path)
    threads = Threads(store)
    await threads.append_messages(KEY, [_msg(1), _msg(2)])
    assert await threads.discard_message(KEY, "m1")
    await SessionPort(store, owner="next").aclaim(KEY.system, KEY.thread)


async def test_facade_write_into_a_busy_thread_is_thread_busy(tmp_path):
    store = FileThreadStore(tmp_path)
    await SessionPort(store, owner="runner").aclaim(KEY.system, KEY.thread)
    with pytest.raises(ThreadBusy):
        await Threads(store).append_messages(KEY, [_msg(1)])
