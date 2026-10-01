"""Run workflow, model and host-affine resource queues."""

import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from gc_core.execution.contracts import ExecutionFailure
from temporalio.client import Client
from temporalio.worker import Worker

from gc_ua_temporalio.activities import AgentActivities
from gc_ua_temporalio.child_activities import ChildActivities
from gc_ua_temporalio.config import TemporalRuntimeConfig
from gc_ua_temporalio.mcp_activities import McpActivities
from gc_ua_temporalio.runtime import WorkerRuntime
from gc_ua_temporalio.session_activities import SessionActivities
from gc_ua_temporalio.summary_activities import SummaryActivities
from gc_ua_temporalio.workflow import AgentSessionWorkflow


async def serve(config):
    client = await Client.connect(config.address, namespace=config.namespace)
    activities = AgentActivities(WorkerRuntime(config))
    sessions = SessionActivities(activities.runtime)
    summaries = SummaryActivities(activities)
    mcp = McpActivities(activities)
    children = ChildActivities(activities)
    async with AsyncExitStack() as stack:
        for options in (
            dict(
                task_queue=config.workflow_queue,
                workflows=[AgentSessionWorkflow],
                workflow_failure_exception_types=[
                    ExecutionFailure,
                    ValueError,
                ],
            ),
            dict(task_queue=config.model_queue, activities=[activities.model]),
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
        ):
            await stack.enter_async_context(Worker(client, **options))
        await asyncio.Event().wait()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True)
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO)
    asyncio.run(serve(TemporalRuntimeConfig.load(args.config)))


if __name__ == "__main__":
    main()
