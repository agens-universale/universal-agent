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

Шаг — один ход агента в дочернем ``UniversalAgentWorkflow`` (тот же агент, тред и
гарантии, что у сессии). ID дочернего workflow предсказуем
(``step_workflow_id``): по нему приложение отвечает на вопрос агента и
останавливает ход — ``AgentSession(..., workflow_id=step_workflow_id(...))``.
Воркер процесса должен нести ``AgentPlugin`` (он регистрирует
``UniversalAgentWorkflow`` и activities агента).

Тред шага занимает дочерний workflow: пока в том же треде идёт сессия
(``AgentSession``) или другой шаг, этот отказывает (тред занят). Родитель
закрылся — по умолчанию Temporal снимает и ребёнка (``ParentClosePolicy.
TERMINATE``), тред освобождается при следующем захвате. Свои параметры
дочернего workflow (``parent_close_policy``, ``execution_timeout``,
``retry_policy``…) — именованными аргументами ``agent_step``, как у
``workflow.execute_child_workflow``.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from gc_core_temporalio import sandbox_passthrough  # noqa: F401

# isort: split
from gc_core.trace_id import normalize_trace_id
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from gc_core_temporalio.contracts import RunRequest, UserMessage
from gc_core_temporalio.sizes import payload_size
from gc_core_temporalio.workflow import UniversalAgentWorkflow


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
    trace_id: str | None = None,
    **child_options: Any,
) -> AgentStepResult:
    """Один ход агента ``agent`` в треде ``thread_id`` — в коде workflow.

    ``step`` — имя шага для ID дочернего workflow (по умолчанию ``thread_id``;
    два шага одного треда подряд могут делить имя — первый к тому времени
    завершён). ``task_queue`` — очередь воркера с ``AgentPlugin``; по
    умолчанию — очередь текущего workflow. ``child_options`` — параметры
    ``workflow.execute_child_workflow`` (``parent_close_policy``,
    ``execution_timeout``, ``retry_policy``…); ``parent_close_policy`` по
    умолчанию — ``REQUEST_CANCEL``: тред освобождается и при terminate
    родителя. Отказ хода — исключение:
    ``ChildWorkflowError`` (сбой сессии) или ``ApplicationError`` типа
    ``TurnFailed`` (ход не уложился в запись истории); слишком большой
    промпт — ``ApplicationError`` типа ``HistoryLimitExceeded`` до запуска.
    ``trace_id`` — внешний трейс шага: заголовком в модель и MCP шага и его
    субагентов; кривое значение — ``ApplicationError`` типа
    ``InvalidTraceId`` до запуска.
    """
    try:
        trace_id = normalize_trace_id(trace_id)
    except ValueError as exc:
        raise ApplicationError(
            str(exc), type="InvalidTraceId", non_retryable=True
        ) from None
    message_id = str(workflow.uuid4())
    try:
        UserMessage(message_id, prompt).validate()
    except ValueError as exc:
        # Исключение не-ApplicationError в коде workflow роняло бы задачу
        # родителя бесконечно; здесь — отказ шага с размером.
        raise ApplicationError(
            f"Промпт шага — {payload_size(prompt)} байт в истории Temporal: "
            f"{exc}",
            type="HistoryLimitExceeded",
            non_retryable=True,
        ) from None
    child_id = step_workflow_id(workflow.info().workflow_id, step or thread_id)
    # Закрылся родитель (конец, terminate, таймаут) — ребёнку отмена, а не
    # terminate: отменённая сессия сама снимает владение тредом, иначе тред
    # оставался бы занятым до ``gc-temporal unlock``.
    child_options.setdefault(
        "parent_close_policy", workflow.ParentClosePolicy.REQUEST_CANCEL
    )
    result = await workflow.execute_child_workflow(
        UniversalAgentWorkflow.run,
        RunRequest(
            agent,
            store_id,
            system_id,
            thread_id,
            [prompt],
            # Детерминированный и уникальный: replay даёт тот же ID, а два
            # шага одного треда не склеиваются дедупликацией.
            message_ids=[message_id],
            format_text=format_text,
            trace_id=trace_id,
        ),
        id=child_id,
        task_queue=task_queue or workflow.info().task_queue,
        **child_options,
    )
    if result.get("turn_error"):
        raise ApplicationError(
            result["turn_error"], type="TurnFailed", non_retryable=True
        )
    return AgentStepResult(
        answer=result.get("answer"),
        terminal_outcome=result.get("terminal_outcome"),
        workflow_id=child_id,
        formatted_answer=result.get("formatted_answer"),
        post_error=result.get("post_error"),
    )


__all__ = ["AgentStepResult", "agent_step", "step_workflow_id"]
