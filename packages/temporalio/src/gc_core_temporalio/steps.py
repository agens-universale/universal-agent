"""Агент шагом своего workflow: ``await agent_step(...)`` в коде workflow.

Свой бизнес-процесс в Temporal (оплата → проверка агентом → письмо) зовёт
агента как обычный шаг — как ``await agent.run(...)`` у Pydantic AI::

    @workflow.defn
    class OrderWorkflow:
        @workflow.run
        async def run(self, order_id: str) -> str:
            await workflow.execute_activity(charge, order_id, ...)
            step = await agent_step(
                "shop", f"Проверь заказ {order_id}", thread_id=f"order-{order_id}"
            )
            await workflow.execute_activity(send_email, step.answer, ...)
            return step.answer

Шаг — один ход агента в дочернем ``AgentWorkflow`` (тот же агент, тред и
гарантии, что у сессии). ID дочернего workflow предсказуем
(``step_workflow_id``): по нему приложение отвечает на вопрос агента и
останавливает ход — ``AgentSession(..., workflow_id=step_workflow_id(...))``.
Воркер процесса должен нести ``AgentPlugin`` (он регистрирует
``AgentWorkflow`` и activities агента).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core_temporalio import sandbox_passthrough  # noqa: F401

# isort: split
from temporalio import workflow

from gc_core_temporalio.contracts import RunRequest
from gc_core_temporalio.workflow import AgentWorkflow


@dataclass
class AgentStepResult:
    """Итог шага: ответ хода и как он закончился."""

    answer: Any
    #: Как закончился ход по мнению ядра (``None`` — обычный финиш).
    terminal_outcome: str | None
    #: ID дочернего workflow шага.
    workflow_id: str
    formatted_answer: Any = None
    post_error: str | None = None


def step_workflow_id(parent_workflow_id: str, step: str) -> str:
    """ID дочернего workflow шага ``step`` (по умолчанию — ID треда)."""
    return f"{parent_workflow_id}/agent/{step}"


async def agent_step(
    agent: str,
    prompt: str,
    *,
    thread_id: str,
    system_id: str = "sdk",
    store_id: str = "default",
    step: str | None = None,
    task_queue: str | None = None,
    format_text: str | None = None,
) -> AgentStepResult:
    """Один ход агента ``agent`` в треде ``thread_id`` — в коде workflow.

    ``step`` — имя шага для ID дочернего workflow (по умолчанию ``thread_id``;
    два шага одного треда подряд могут делить имя — первый к тому времени
    завершён). ``task_queue`` — очередь воркера с ``AgentPlugin``; по
    умолчанию — очередь текущего workflow. Отказ хода — исключение
    дочернего workflow (``ChildWorkflowError``).
    """
    child_id = step_workflow_id(workflow.info().workflow_id, step or thread_id)
    result = await workflow.execute_child_workflow(
        AgentWorkflow.run,
        RunRequest(
            agent,
            store_id,
            system_id,
            thread_id,
            [prompt],
            # Детерминированный и уникальный: replay даёт тот же ID, а два
            # шага одного треда не склеиваются дедупликацией.
            message_ids=[str(workflow.uuid4())],
            format_text=format_text,
        ),
        id=child_id,
        task_queue=task_queue or workflow.info().task_queue,
    )
    return AgentStepResult(
        answer=result.get("answer"),
        terminal_outcome=result.get("terminal_outcome"),
        workflow_id=child_id,
        formatted_answer=result.get("formatted_answer"),
        post_error=result.get("post_error"),
    )


__all__ = ["AgentStepResult", "agent_step", "step_workflow_id"]
