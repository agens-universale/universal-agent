"""Обслуживание воркера освобождает брошенное и чистит служебные данные."""

import time

import test_workflow_controls as controls
from gc_core_temporalio import maintenance
from gc_core_temporalio.maintenance import (
    WorkerMaintenance,
    owner_workflow,
)
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from gc_stores.sqlite import SQLiteThreadStore
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


async def test_old_store_receipts_are_pruned(tmp_path):
    """Квитанции хранилищ тредов старше недели чистятся: повтор activity —
    минуты, а через дни квитанция только занимает место."""
    from datetime import UTC, datetime, timedelta

    from gc_core.threads.models import LeaseRequest, SideChange, ThreadKey

    now = [datetime(2026, 1, 1, tzinfo=UTC)]
    store = SQLiteThreadStore(
        tmp_path / "threads.sqlite", clock=lambda: now[0]
    )
    key = ThreadKey(system="s", thread="t")
    await store.claim(key, LeaseRequest(owner="w"))
    await store.append_side(key, SideChange(op_id="old"))
    now[0] += timedelta(days=30)
    await store.append_side(key, SideChange(op_id="new"))

    class Idle:
        def idle_owners(self, _seconds):
            return []

    await WorkerMaintenance(None, Idle(), {"threads": store}).run_once()
    now[0] += timedelta(seconds=1)
    assert await store.prune_receipts(timedelta(0)) == 1, "свежая осталась"


def test_owner_state_is_unknown_without_reliable_facts():
    from gc_core_temporalio.maintenance import owner_state

    workflows = {"wf": ("running", None), "old": ("running", "run-2")}
    # Сервер не дал first_run_id — живой ход не трогать.
    assert owner_state("wf/run-1/turn-1", workflows) == "running"
    assert owner_state("old/run-1/turn-3", workflows) == "closed"
    # describe не удался — состояние неизвестно.
    assert owner_state("missing/run/turn-1", workflows) is None
