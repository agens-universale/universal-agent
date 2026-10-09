"""Свой workflow с агентом шагом — для test_steps_live (минимум импортов:
модуль загружается и в песочнице Temporal)."""

from gc_core_temporalio.steps import agent_step
from temporalio import workflow


@workflow.defn
class OrderWorkflow:
    @workflow.run
    async def run(self, order_id: str) -> dict:
        first = await agent_step(
            "shop", f"Проверь заказ {order_id}", thread_id=f"order-{order_id}"
        )
        second = await agent_step(
            "shop", "А доставка?", thread_id=f"order-{order_id}"
        )
        return {
            "first": first.answer,
            "second": second.answer,
            "step_id": second.workflow_id,
        }
