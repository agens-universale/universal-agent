"""Тред, которым владеет durable-исполнитель (Temporal: бессрочная аренда v2,
в ``control.json`` — ``owner_id``), закрыт для локального писателя и для
чужого захвата; сам исполнитель пишет в него и сбоку (статус, трассы)."""

import asyncio
from datetime import timedelta

import pytest
from gc_core.thread_store import ThreadSession
from gc_core.thread_store_journal import durable_owner_id
from gc_core.threads.errors import OwnershipError
from gc_core.threads.file_store import FileThreadStore
from gc_core.threads.models import LeaseRequest, ThreadKey

KEY = ThreadKey(system="cli", thread="t")


def _owned_by_executor(tmp_path, owner="wf-1"):
    store = FileThreadStore(tmp_path)
    return asyncio.run(store.claim(KEY, LeaseRequest(owner=owner))).lease


def test_local_session_refuses_thread_owned_by_durable_executor(tmp_path):
    _owned_by_executor(tmp_path)
    with pytest.raises(OwnershipError, match="durable executor"):
        asyncio.run(
            ThreadSession.afrom_config(
                settings=None,
                system_id="cli",
                thread_id="t",
                store=FileThreadStore(tmp_path),
            )
        )


def test_claim_refuses_thread_of_durable_executor(tmp_path):
    _owned_by_executor(tmp_path, owner="wf-1")
    store = FileThreadStore(tmp_path)
    with pytest.raises(OwnershipError):
        asyncio.run(
            store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(60)))
        )
    record = asyncio.run(store.claim(KEY, LeaseRequest(owner="wf-1")))
    assert record.lease.owner == "wf-1"


def test_untimed_lease_is_visible_to_develop_writers(tmp_path):
    lease = _owned_by_executor(tmp_path, owner="wf-2")
    assert durable_owner_id(str(tmp_path), "cli", "t") == "wf-2"
    asyncio.run(FileThreadStore(tmp_path).release(KEY, lease))
    assert durable_owner_id(str(tmp_path), "cli", "t") is None


def test_timed_lease_does_not_lock_out_develop_writers(tmp_path):
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.claim(KEY, LeaseRequest(owner="host", ttl=timedelta(60)))
    )
    assert durable_owner_id(str(tmp_path), "cli", "t") is None


def test_delete_refuses_thread_of_durable_executor(tmp_path):
    _owned_by_executor(tmp_path, owner="wf-1")
    (tmp_path / "data_cli_t.jsonl").write_text("", encoding="utf-8")
    store = FileThreadStore(tmp_path)
    with pytest.raises(OwnershipError):
        asyncio.run(store.delete(KEY, op_id="janitor"))


def test_only_the_executor_writes_status_beside_its_turn(tmp_path):
    """Статус хода, трассу и алиасы треда durable-исполнителя сбоку пишет
    только он (по аренде); состояние фронтенда — любой процесс."""
    from gc_core.threads.models import RuntimeStatus, SideChange

    lease = _owned_by_executor(tmp_path, owner="wf-1")
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.append_side(
            KEY, SideChange(op_id="tui", patch_frontend=("tui", {"x": 1}))
        )
    )
    for change in (
        SideChange(op_id="rt", set_runtime=RuntimeStatus(turn="running")),
        SideChange(op_id="tr", append=[("subagent_events", [{"k": 1}])]),
        SideChange(op_id="al", add_aliases=["resp_1"]),
    ):
        with pytest.raises(OwnershipError):
            asyncio.run(store.append_side(KEY, change))
        asyncio.run(
            store.append_side(
                KEY,
                change.model_copy(
                    update={"op_id": "own-" + change.op_id, "lease": lease}
                ),
            )
        )
    record = asyncio.run(store.get_thread(KEY))
    assert record.runtime.turn == "running"
    assert asyncio.run(store.resolve_alias("resp_1")) == KEY


def test_reset_history_frees_the_thread_for_develop_writers(tmp_path):
    """Сброс истории снимает аренду — и отметку durable-владельца: тред не
    остаётся закрытым без аренды, которую можно снять."""
    from gc_core.threads.models import ThreadChange

    lease = _owned_by_executor(tmp_path, owner="wf-1")
    store = FileThreadStore(tmp_path)
    asyncio.run(
        store.commit(
            KEY,
            ThreadChange(
                op_id="reset",
                expected_revision=0,
                lease=lease,
                reset_history=True,
            ),
        )
    )
    assert durable_owner_id(str(tmp_path), "cli", "t") is None
