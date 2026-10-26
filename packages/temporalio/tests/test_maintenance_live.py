"""Обслуживание воркера освобождает брошенное и чистит служебные данные."""

import os
import time

import test_workflow_controls as controls
from gc_core.thread_store_journal import prune_durable_receipts
from gc_core_temporalio import maintenance
from gc_core_temporalio.maintenance import (
    WorkerMaintenance,
    owner_workflow,
)
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from gc_core_temporalio.storage.sqlite import SQLiteThreadStore
from live_support import call
from test_workflow_controls import finish, request, runtime, wait_status

scripted_http = controls.scripted_http


def test_owner_workflow_parses_turn_and_child_owners():
    assert owner_workflow("wf/run-1/turn-3") == ("wf", "run-1")
    assert owner_workflow("wf/child_abc") == ("wf/child_abc", None)


async def test_terminated_turn_context_is_released(
    tmp_path, monkeypatch, scripted_http
):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(maintenance, "IDLE_SECONDS", 0)
    url, requests, script = scripted_http
    (tmp_path / "a.txt").write_text("a")

    def slow():
        time.sleep(3.0)
        return [call("r", "fs_read_file", {"path": "a.txt"})]

    script.extend([slow, [finish("done")]])
    async with runtime(tmp_path, url) as (env, provider, config):
        handle = await env.client.start_workflow(
            UniversalAgentWorkflow.run,
            request(),
            id="controls",
            task_queue="native-controls",
        )
        await wait_status(
            handle, lambda s: s["phase"] == "running" and requests
        )
        await handle.terminate("operator")
        assert provider.idle_owners(0)
        await WorkerMaintenance(
            env.client, provider, provider.stores
        ).run_once()
        assert provider.idle_owners(0) == []


def test_old_store_receipts_are_pruned(tmp_path):
    receipts = tmp_path / ".store" / "abc" / "receipts"
    receipts.mkdir(parents=True)
    old, fresh = receipts / "old.json", receipts / "fresh.json"
    old.write_text("{}")
    fresh.write_text("{}")
    stale = time.time() - 30 * 24 * 3600
    os.utime(old, (stale, stale))
    assert prune_durable_receipts(tmp_path, max_age_seconds=3600) == 1
    assert fresh.exists() and not old.exists()

    store = SQLiteThreadStore(str(tmp_path / "threads.sqlite"))
    store.ensure_ready()
    with store.transaction() as db:
        db.execute(
            "INSERT INTO durable_receipts(system,thread,operation,digest,"
            "receipt,created) VALUES ('s','t','old','d','{}',?),"
            "('s','t','new','d','{}',?)",
            (stale, time.time()),
        )
    assert store.prune_receipts(max_age_seconds=3600) == 1


def test_owner_state_is_unknown_without_reliable_facts():
    from gc_core_temporalio.maintenance import owner_state

    workflows = {"wf": ("running", None), "old": ("running", "run-2")}
    # Сервер не дал first_run_id — живой ход не трогать.
    assert owner_state("wf/run-1/turn-1", workflows) == "running"
    assert owner_state("old/run-1/turn-3", workflows) == "closed"
    # describe не удался — состояние неизвестно.
    assert owner_state("missing/run/turn-1", workflows) is None
