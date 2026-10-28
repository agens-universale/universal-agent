"""Parent-side delivery and acknowledgement of the common child runner."""

import asyncio
import dataclasses
from copy import deepcopy
from dataclasses import replace

from gc_core.context_preparation import PreparedRunContext
from gc_core.execution.contracts import (
    ExecutionFailure,
    InfrastructureFailure,
)
from gc_core.execution.human import HumanRequest, HumanRequestInterrupted
from gc_core.security.action_judge import words_within_budget
from gc_core.tools.subagents.runner_transfer import (
    apply_subagent_transfer,
    capture_subagent_transfer,
)
from temporalio import workflow
from temporalio.converter import value_to_type
from temporalio.exceptions import ApplicationError, ChildWorkflowError

from gc_core_temporalio.child_contracts import (
    ChildCheckpoint,
    ChildRequest,
    ChildResult,
)
from gc_core_temporalio.operations import restore_domain_error
from gc_core_temporalio.public_config import public_config_snapshot
from gc_core_temporalio.sizes import check_history_size


class RemoteChildInput:
    def __init__(self, parent_call_id):
        self.parent_call_id = parent_call_id
        self.handle = None
        self.pending = []
        self.tasks = []

    def enqueue(self, message):
        self.pending.append(deepcopy(message))
        self.flush()

    def flush(self):
        if self.handle is not None:
            pending, self.pending = self.pending, []
            for message in pending:
                self.tasks.append(
                    (
                        message,
                        asyncio.create_task(
                            self.handle.signal("submit_message", message)
                        ),
                    )
                )

    async def drain_deliveries(self):
        """Дождаться сигналов; вернуть сообщения, которые ребёнок не получил.

        Ребёнок мог завершиться раньше доставки: это не ошибка хода, но
        сообщение уже принято сессией — оно возвращается родителю.
        """
        tasks, self.tasks = self.tasks, []
        if not tasks:
            return []
        results = await asyncio.gather(
            *(task for _, task in tasks), return_exceptions=True
        )
        return [
            message
            for (message, _), result in zip(tasks, results)
            if isinstance(result, BaseException)
        ]


#: Поля состояния родителя, которые субагенту не нужны и тяжелы: переписка
#: родителя, хранилище полных выводов инструментов (до 50 записей по 1 МБ),
#: журнал намерений. Код субагента читает из состояния родителя только
#: usage, stop_requested, state_id, default_model и системный промпт; общие
#: данные (память, todo) идут отдельным переносом ``shared``.
_PARENT_STATE_HEAVY = {
    "messages": [],
    "user_intent_log": [],
    "current_user_intent": [],
}


def _light_parent_snapshot(state):
    """Снимок состояния родителя для входа субагента без тяжёлых полей:
    вход child workflow и ``universal_agent.subagent.prepare`` — записи истории."""
    from gc_core.execution.output_store import ToolCallOutputsStore

    snapshot = state.to_execution_snapshot()
    data = snapshot["data"]
    for name, empty in _PARENT_STATE_HEAVY.items():
        if name in data:
            data[name] = list(empty)
    if "tool_call_outputs_store" in data:
        data["tool_call_outputs_store"] = ToolCallOutputsStore().to_dict()
    return snapshot


class ChildTransport:
    def __init__(self, ctx, start, prepared, store, operations, human_request):
        self.ctx, self.start, self.prepared = ctx, start, prepared
        self.store, self.operations, self.human_request = (
            store,
            operations,
            human_request,
        )
        self.active_child = None
        self._handle = None
        self._state = None
        self._last = None
        self._sequence = 0
        #: Доставка сообщения в цикл родителя (``AgentExecutor.add_message``).
        self.redeliver = None
        #: Сбой применения checkpoint из сигнала: поднимается в ``execute``,
        #: а не в обработчике сигнала (там он валил бы весь workflow).
        self.checkpoint_failure = None

    def receive_checkpoint(self, payload):
        """Обработчик сигнала ``child_checkpoint``: сбой — в ход, не в сессию."""
        try:
            self.apply_checkpoint(payload)
        except Exception as exc:
            if self.checkpoint_failure is None:
                self.checkpoint_failure = exc

    def apply_checkpoint(self, payload):
        checkpoint = (
            value_to_type(ChildCheckpoint, payload)
            if isinstance(payload, dict)
            else payload
        )
        if checkpoint.child_id != self.active_child:
            return
        if checkpoint.sequence <= self._sequence:
            return
        apply_subagent_transfer(
            self._state, self.ctx, self._last, checkpoint.state
        )
        self._last, self._sequence = checkpoint.state, checkpoint.sequence

    async def answer_child_human(self, payload):
        child_id = payload["child_id"]
        # Сигнал ребёнка может прийти в одной активации со стартом child
        # workflow, раньше, чем родитель получил handle.
        await workflow.wait_condition(
            lambda: self.active_child != child_id or self._handle is not None
        )
        if self.active_child != child_id:
            workflow.logger.warning("Human request from an inactive child")
            return
        try:
            # Ребёнок, завершившийся во время ожидания, снимает свой вопрос:
            # иначе обработчик сигнала держал бы сессию до stop.
            answer = await self.human_request(
                value_to_type(HumanRequest, payload["request"]),
                abandon=lambda: self.active_child != child_id,
            )
        except HumanRequestInterrupted:
            # Ответа не будет (человека нет, вопрос снят): живому ребёнку —
            # отказ, иначе он ждал бы вечно, а с ним и ход родителя.
            if self.active_child == child_id and not (
                self._state is not None and self._state.stop_requested
            ):
                try:
                    await self._handle.signal(
                        "refuse_human", args=[payload["request_id"]]
                    )
                except Exception:  # ребёнок уже закрыт — отказывать некому
                    workflow.logger.warning(
                        "Child %s closed before the refusal", child_id
                    )
            return
        if self._state is None or self._state.stop_requested:
            return
        await self._handle.signal(
            "answer_human", args=[payload["request_id"], answer]
        )

    async def stop_child(self):
        if self._handle is not None:
            await self._handle.signal("stop")

    def _child_context(self, tool_name, runner):
        """Контекст входа ребёнка. Зовёт ``workflow.patched``: место вызова в
        ``execute`` (до снимка родителя и старта child workflow) не менять."""
        router = self.ctx.model_router
        parent = self.operations.child_context or {}
        child_context = {
            # Корневая сессия (workflow ID верхнего workflow, стабилен через
            # Continue-As-New): субагенты делят с ней кэш страниц воркера.
            "session_id": parent.get("session_id")
            or workflow.info().workflow_id,
            "parent_owner_id": self.operations.owner_id,
            "parent_context": self.operations.child_context,
            "tool_name": tool_name,
            "frozen_level": router.active_level(),
            "force_legacy": router.force_legacy,
            "circuit_breaker": router.breaker.snapshot(),
        }
        # Старые истории сохраняют прежний вход child workflow; новые
        # передают AUTO отдельно от уровня уже выбранного вызова родителя.
        if workflow.patched("subagent-frozen-live-level-v1"):
            child_context["frozen_live_level"] = router.live_level()
        if workflow.patched("subagent-independent-auto-v1"):
            child_context["independent_auto"] = True
        # Слова человека корня — данными: лёгкий снимок родителя переписки и
        # журнала не несёт, а судье действий ребёнка они нужны. Урезаны тем
        # же правилом, что в запросе судьи: вход ребёнка ограничен размером.
        if workflow.patched("subagent-root-user-words-v1"):
            child_context["root_user_words"] = words_within_budget(
                runner._root_user_words()
            )
        return child_context

    async def execute(self, runner, tool_name, arguments, *, parent_call_id):
        if self.active_child is not None:
            raise ExecutionFailure(
                "A child is already active for this context"
            )
        child_id = (
            f"{workflow.info().workflow_id}/{self.operations.new_id('child')}"
        )
        self._state = runner._orchestrator_state
        self._last, self._sequence = (
            capture_subagent_transfer(self._state, self.ctx),
            0,
        )
        # Снимок настроек родителя — тот же публичный дамп, что пришёл с
        # воркера: настройки workflow собраны из него и неизменяемы.
        public = public_config_snapshot(self.ctx)
        prepared = replace(
            self.prepared,
            context=PreparedRunContext.from_context(
                self.ctx, settings=public.settings, runtime=public.runtime
            ),
            resource_owner=self.operations.resource_owner,
        )
        child_context = self._child_context(tool_name, runner)
        try:
            parent_snapshot = _light_parent_snapshot(self._state)
        except ValueError as exc:
            raise ExecutionFailure(
                "Parent state cannot cross the child execution boundary"
            ) from exc
        request = ChildRequest(
            self.start,
            workflow.info().workflow_id,
            prepared,
            parent_snapshot,
            self._last,
            child_context,
            tool_name,
            arguments,
            parent_call_id,
            *_lease_parts(self.store),
        )
        # Вход дочернего workflow — запись истории родителя: больше
        # лимита — сервер отверг бы команду, и задача родителя падала бы
        # бесконечно. Ошибка вместо вечного цикла.
        check_history_size(
            f"Вход субагента {tool_name}", dataclasses.asdict(request)
        )
        self.active_child = child_id
        self.checkpoint_failure = None
        scope = RemoteChildInput(parent_call_id)
        undelivered = []
        previous_scope = getattr(self.ctx, "child_input_scope", None)
        self.ctx.child_input_scope = scope
        try:
            self._handle = await workflow.start_child_workflow(
                "UniversalSubagentWorkflow",
                request,
                id=child_id,
                result_type=ChildResult,
                cancellation_type=workflow.ChildWorkflowCancellationType.WAIT_CANCELLATION_COMPLETED,
            )
            scope.handle = self._handle
            scope.flush()
            if self._state.stop_requested:
                await self.stop_child()
            try:
                result = await self._handle
            except ChildWorkflowError as exc:
                if not isinstance(exc.cause, ApplicationError):
                    # terminate/таймаут ребёнка извне — ошибка инструмента,
                    # а не отказ всей сессии.
                    return runner._error_response(
                        tool_name=tool_name,
                        code="subagent_interrupted",
                        message=(
                            "Субагент прерван извне; часть его действий могла "
                            "выполниться. Проверьте состояние, прежде чем "
                            "повторять."
                        ),
                        retryable=False,
                    )
                raise _child_error(exc) from exc
            finally:
                # Дальнейший ввод больше не уходит ребёнку.
                scope.handle = None
            undelivered = result.undelivered
            if self.checkpoint_failure is not None:
                raise ExecutionFailure(
                    "Child checkpoint cannot be applied to the parent"
                ) from self.checkpoint_failure
            self.apply_checkpoint(result.checkpoint)
            return result.output
        finally:
            if getattr(self.ctx, "child_input_scope", None) is scope:
                self.ctx.child_input_scope = previous_scope
            try:
                failed = await scope.drain_deliveries()
                late = [*undelivered, *failed, *scope.pending]
                scope.pending = []
                if late and self.redeliver is not None:
                    for message in late:
                        self.redeliver(message)
            finally:
                self.active_child = self._handle = self._state = self._last = (
                    None
                )


def _child_error(exc):
    cause = exc.cause
    if isinstance(cause, ApplicationError):
        if cause.type == "DomainError":
            return restore_domain_error(cause)
        if cause.type in ("ExecutionFailure", "InfrastructureFailure"):
            return ExecutionFailure(cause.message)
    return InfrastructureFailure("Child workflow failed")


def install_subagents(
    ctx, start, prepared, store, operations, *, human_request
):
    transport = ChildTransport(
        ctx, start, prepared, store, operations, human_request
    )
    ctx.subagent_executor = transport.execute
    operations.stop_child = transport.stop_child
    return transport


def _lease_parts(store) -> tuple[str | None, int | None, str | None]:
    """Владелец, эпоха и токен аренды треда сессии (нет аренды — ``None``)."""
    lease = getattr(store, "lease", None)
    if lease is None:
        return None, None, None
    return lease.owner, lease.epoch, lease.token
