"""Subprocess worker for real crash/replay tests. Gates never enter production."""

import asyncio
import sys
from contextlib import AsyncExitStack
from pathlib import Path

from gc_core.execution.contracts import ExecutionFailure
from gc_ua_temporalio.activities import AgentActivities
from gc_ua_temporalio.child_activities import ChildActivities
from gc_ua_temporalio.config import TemporalRuntimeConfig
from gc_ua_temporalio.mcp_activities import McpActivities
from gc_ua_temporalio.runtime import WorkerRuntime
from gc_ua_temporalio.session_activities import SessionActivities
from gc_ua_temporalio.summary_activities import SummaryActivities
from gc_ua_temporalio.workflow import AgentSessionWorkflow
from temporalio.client import Client
from temporalio.worker import Worker


class GatedRuntime(WorkerRuntime):
    async def gate(self, kind):
        if sys.argv[2] != kind:
            return
        Path(sys.argv[3]).write_text(kind)
        await asyncio.Event().wait()

    async def tool(self, data):
        await self.gate("before_tool")
        with (
            Path(sys.argv[1])
            .with_name("executed-tools.txt")
            .open("a") as stream
        ):
            stream.write(data.invocation.name + "\n")
        return await super().tool(data)

    async def model(self, data):
        if data.request.operation_id != "model-1":
            await self.gate("after_tools")
        return await super().model(data)

    async def after_effect(self, data):
        await self.gate("after_effect")


class GatedSummaries(SummaryActivities):
    async def _prepare(self, data):
        result = await super()._prepare(data)
        if result.patch:
            await self.runtime.gate("after_summary")
        return result


class GatedMcp(McpActivities):
    async def _invoke(self, data):
        result = await super()._invoke(data)
        await self.runtime.gate("after_mcp")
        return result


async def main():
    config = TemporalRuntimeConfig.load(sys.argv[1])
    activities = AgentActivities(GatedRuntime(config))
    activities.guarded.after_effect = activities.runtime.after_effect
    sessions = SessionActivities(activities.runtime)
    summaries = GatedSummaries(activities)
    mcp = GatedMcp(activities)
    children = ChildActivities(activities)
    client = await Client.connect(config.address, namespace=config.namespace)
    async with AsyncExitStack() as stack:
        for opts in (
            dict(
                task_queue=config.workflow_queue,
                workflows=[AgentSessionWorkflow],
                workflow_failure_exception_types=[
                    ExecutionFailure,
                    ValueError,
                ],
            ),
            dict(
                task_queue=config.resource_queue,
                activities=[
                    activities.bootstrap,
                    activities.tool,
                    activities.persist,
                    activities.prepare,
                    activities.effect,
                    activities.reconcile,
                    sessions.accept,
                    sessions.finish,
                    sessions.checkpoint,
                    summaries.prepare,
                    mcp.invoke,
                    mcp.reconcile,
                    mcp.release,
                    children.prepare,
                ],
            ),
            dict(task_queue=config.model_queue, activities=[activities.model]),
        ):
            await stack.enter_async_context(Worker(client, **opts))
        await asyncio.Event().wait()


asyncio.run(main())
