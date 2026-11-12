"""Сброс флагов хода на старте прогона — запись сбоку из снимка сессии.

Флаги ``summarizing`` / ``finalizing`` / ``finish_gate_checking``,
залипшие после падения прошлого прогона, снимаются; прочие поля живого
статуса и секции фронтендов (их пишут сами фронтенды) остаются."""

import pytest
from gc_core.run_summary_coordinator import (
    areset_run_flags,
    aset_tui_finalizing,
    aset_tui_finish_gate_checking,
)
from gc_core.thread_store import ThreadSession
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import RuntimeStatus, SideChange, ThreadKey
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore

KEY = ThreadKey(system="s", thread="t")


def _store(kind, tmp_path):
    if kind == "file":
        return FileThreadStore(tmp_path / "threads")
    return MemoryThreadStore()


async def _seed(store, flags, **extra):
    await store.append_side(
        KEY,
        SideChange(
            op_id="seed",
            set_runtime=RuntimeStatus(flags=frozenset(flags), **extra),
            set_frontend=("tui", {"pin": 1}),
        ),
    )


async def _open(store):
    return await ThreadSession.afrom_config(
        settings=None, system_id=KEY.system, thread_id=KEY.thread, store=store
    )


@pytest.mark.parametrize("kind", ["file", "memory"])
@pytest.mark.parametrize("summarizing", [False, True])
async def test_reset_clears_run_flags_and_keeps_the_rest(
    tmp_path, kind, summarizing
):
    store = _store(kind, tmp_path)
    flags = {"finalizing", "finish_gate_checking"}
    if summarizing:
        flags.add("summarizing")
    await _seed(store, flags, external=9)
    session = await _open(store)
    await areset_run_flags(session)
    record = await store.get_thread(KEY)
    assert record.runtime.flags == frozenset()
    assert record.runtime.model_extra == {"external": 9}
    assert record.frontend["tui"] == {"pin": 1}
    assert session.runtime == record.runtime


async def test_reset_matches_flag_by_flag_clearing(tmp_path):
    """Сброс разом даёт то же, что снятие каждого флага по отдельности."""
    flags = {"summarizing", "finalizing", "finish_gate_checking"}
    batch, single = MemoryThreadStore(), MemoryThreadStore()
    for store in (batch, single):
        await _seed(store, flags, external=9)
    await areset_run_flags(await _open(batch))
    session = await _open(single)
    await session.aset_flag("summarizing", active=False)
    await aset_tui_finalizing(session, False)
    await aset_tui_finish_gate_checking(session, False)
    first, second = [
        (await s.get_thread(KEY)).runtime for s in (batch, single)
    ]
    assert first == second


async def test_frontend_written_after_open_survives_reset(tmp_path):
    store = FileThreadStore(tmp_path)
    await _seed(store, {"summarizing"})
    session = await _open(store)
    # Фронтенд пишет свою секцию после открытия сессии — своей записью.
    await store.append_side(
        KEY,
        SideChange(
            op_id="tui", set_frontend=("tui", {"pin": 2, "draft": "new"})
        ),
    )
    await areset_run_flags(session)
    record = await store.get_thread(KEY)
    assert record.frontend["tui"] == {"pin": 2, "draft": "new"}
    assert not record.runtime.flags


async def test_reset_without_run_flags_writes_nothing(tmp_path):
    store = MemoryThreadStore()
    await _seed(store, set(), external=9)
    session = await _open(store)
    writes: list = []
    real = store.append_side

    async def counted(key, change):
        writes.append(change)
        return await real(key, change)

    store.append_side = counted
    await areset_run_flags(session)
    assert writes == []


async def test_reset_none_session_is_noop():
    await areset_run_flags(None)
