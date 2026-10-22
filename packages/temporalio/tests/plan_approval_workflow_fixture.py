"""Workflow сессии, одобряющий предложенный план перед вторым ходом, — для
test_plan_cycle_live (минимум импортов: модуль загружается и в песочнице
Temporal).

Публичного одобрения плана под Temporal нет. Фронтенды одобряют через
исполнителя (`set_permission_mode` + `accept_plan`), а исполнитель живёт в
workflow и создаётся на каждый ход. Фикстура делает эти два вызова на
подготовке второго хода; остальное — обычный код ядра в песочнице.
"""

from gc_core_temporalio import sandbox_passthrough  # noqa: F401
from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.workflow import UniversalAgentWorkflow
from temporalio import workflow

#: Режим, в котором исполняется одобренный план.
APPROVED_MODE = "edit"


@workflow.defn(name="UniversalAgentWorkflow")
class PlanApprovingWorkflow(UniversalAgentWorkflow):
    @workflow.run
    async def run(self, start: RunRequest) -> dict:
        return await super().run(start)

    async def _prepare_turn(self, start, owner, store):
        store = await super()._prepare_turn(start, owner, store)
        if self.turns == 1:
            self.executor.set_permission_mode(APPROVED_MODE)
            self.executor.accept_plan(APPROVED_MODE, start.thread_id)
        return store
