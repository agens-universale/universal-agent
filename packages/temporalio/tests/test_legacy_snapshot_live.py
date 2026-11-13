"""Старые настройки из истории читаются в workflow без доступа к окружению."""

import asyncio

from gc_core_temporalio import sandbox_passthrough  # noqa: F401

# isort: split
from gc_core.context_preparation import settings_from_snapshot
from gc_core_temporalio.worker import workflow_runner
from live_support import temporal_cli
from temporalio import workflow
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Replayer, Worker


@workflow.defn
class ReadLegacySnapshot:
    @workflow.run
    async def run(self, snapshot: dict) -> str:
        loaded = settings_from_snapshot(snapshot, {"launch_root": "/archived"})
        return loaded.settings.model


async def test_legacy_level_snapshot_loads_and_replays():
    async with await WorkflowEnvironment.start_local(
        dev_server_existing_path=temporal_cli()
    ) as env:
        async with Worker(
            env.client,
            task_queue="legacy-snapshot",
            workflows=[ReadLegacySnapshot],
            workflow_runner=workflow_runner(),
        ):
            handle = await env.client.start_workflow(
                ReadLegacySnapshot.run,
                {"config_version": 1, "level": "auto"},
                id="legacy-snapshot",
                task_queue="legacy-snapshot",
            )
            assert await asyncio.wait_for(handle.result(), 30) == "auto"
            history = await handle.fetch_history()
    await Replayer(
        workflows=[ReadLegacySnapshot], workflow_runner=workflow_runner()
    ).replay_workflow(history)
