"""Шаги агента со своим trace_id — для test_trace_id_live (минимум импортов:
модуль загружается и в песочнице Temporal)."""

from gc_core_temporalio.steps import agent_step
from temporalio import workflow


@workflow.defn
class TracedWorkflow:
    @workflow.run
    async def run(self, traces: list) -> list:
        answers = []
        for index, trace in enumerate(traces):
            step = await agent_step(
                "shop",
                f"Шаг {index}",
                thread_id="traced",
                step=f"run-{index}",
                trace_id=trace,
            )
            answers.append(step.answer)
        return answers
