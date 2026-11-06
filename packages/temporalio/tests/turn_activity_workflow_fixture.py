"""Бизнес-workflow с целым ходом SDK в одной обычной activity."""

from datetime import timedelta

from gc_core_temporalio.turn_activity_contracts import (
    AgentActivityRequest,
    AgentActivityResult,
)
from temporalio import workflow
from temporalio.common import RetryPolicy


@workflow.defn
class TurnActivityWorkflow:
    @workflow.run
    async def run(self, request: AgentActivityRequest) -> AgentActivityResult:
        return await workflow.execute_activity(
            "universal_agent.run",
            request,
            result_type=AgentActivityResult,
            start_to_close_timeout=timedelta(seconds=30),
            heartbeat_timeout=timedelta(seconds=5),
            cancellation_type=workflow.ActivityCancellationType.WAIT_CANCELLATION_COMPLETED,
            retry_policy=RetryPolicy(
                maximum_attempts=3, initial_interval=timedelta(milliseconds=50)
            ),
        )
