"""Temporal delivery and waiting around the ordinary AgentExecutor lifecycle."""

import asyncio
import hashlib
from dataclasses import asdict, replace
from datetime import timedelta

# Сторонние транспорты — из процесса воркера, до первого импорта ядра.
from gc_core_temporalio import sandbox_passthrough  # noqa: F401

# isort: split
from gc_core.agent_executor import AgentExecutor
from gc_core.execution.contracts import ModelRequestStopped
from gc_core.execution.environment import execution_environment
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.security.confirm_port import (
    CONFIRM_ALLOW_ONCE,
    CONFIRM_ALLOW_REMEMBER,
    CONFIRM_DENY_ONCE,
    CONFIRM_DENY_REMEMBER,
)
from gc_core.structured_output import (
    StructuredOutputError,
    arephrase_turn_outcome,
)
from gc_core.threads.models import Lease, LeaseRequest, ThreadKey
from temporalio import workflow
from temporalio.exceptions import ApplicationError

from gc_core_temporalio.child_transport import install_subagents
from gc_core_temporalio.context import assemble_context
from gc_core_temporalio.contracts import (
    PreparedContext,
    RunRequest,
    SessionResume,
    UserMessage,
)
from gc_core_temporalio.operations import (
    WorkflowOperations,
    session_failure,
)
from gc_core_temporalio.public_config import fingerprint_mismatch
from gc_core_temporalio.sizes import (
    ACTIVITY_INPUT_LIMIT,
    HistoryLimitExceeded,
    cut_to_payload,
    payload_size,
)
from gc_core_temporalio.thread_store_proxy import TemporalThreadStore
from gc_core_temporalio.thread_store_transport import ThreadStoreTransport

#: Сколько последних ходов хранят свой ответ для ``message_state``.
TURN_OUTCOMES_KEPT = 8

#: Как часто простаивающая сессия делает Continue-As-New (см. _run_session).
IDLE_REFRESH = timedelta(days=1)

#: Сколько последних ID сообщений и ответов помнить для дедупликации.
DEDUP_WINDOW = 1000

#: Отказ приёма: тот же ID сообщения пришёл с другим текстом.
_MESSAGE_ID_REUSED = "Message ID reused"


def _new_build_announced():
    """Worker Versioning: текущей объявлена другая сборка (без versioning — нет)."""
    info = workflow.info()
    return (
        info.get_current_deployment_version() is not None
        and info.is_target_worker_deployment_version_changed()
    )


def _message_digest(text):
    return hashlib.sha256(text.encode()).hexdigest()


#: Потолок входа Continue-As-New: запись истории (лимит 2 МБ) с запасом.
RESUME_BUDGET = 1_500_000
#: Ответ последнего хода переживает Continue-As-New целиком до стольких байт:
#: CAN идёт сразу после хода, и ``wait``/``ask`` читают ответ уже из нового
#: run. Больше — укорочен с пометкой ``answer_truncated`` (клиент поднимает
#: ``AnswerTruncated``), полный — последним сообщением в треде.
LATEST_ANSWER_KEEP_BYTES = 1_000_000
#: Ответы прошлых ходов и ``status``/результат workflow — не тяжелее.
ANSWER_KEEP_BYTES = 256 * 1024
_ANSWER_FIELDS = ("answer", "formatted_answer", "post_error")


def _bounded_outcome(outcome, limit=ANSWER_KEEP_BYTES):
    """Итог хода для переноса: длинный ответ укорочен с пометкой
    ``answer_truncated`` (полный — последним сообщением в треде)."""
    out = dict(outcome)
    for field in _ANSWER_FIELDS:
        value = out.get(field)
        if value is None or payload_size(value) <= limit:
            continue
        out[field] = (
            cut_to_payload(value, limit) if isinstance(value, str) else None
        )
        out["answer_truncated"] = True
    return out


def _fit_resume(resume):
    """Состояние для Continue-As-New в пределах ``RESUME_BUDGET``.

    Вход CAN — одна запись истории: больше лимита — сервер отвергает команду,
    и задача workflow падала бы бесконечно (сессия встала бы). Ответ
    последнего хода сохраняется как можно полнее; сжимается то, без чего
    сессия работает: ответы прошлых ходов (полные — в треде), тексты
    отброшенных остановкой сообщений (нужны только их ID), старые итоги ходов
    и ответы человеку.
    """
    latest = max(resume.turn_outcomes, key=int, default=None)
    current = {field: getattr(resume, field) for field in _ANSWER_FIELDS}
    last = resume.turn_outcomes.get(latest) if latest is not None else None
    if last is not None and all(
        last.get(field) == value for field, value in current.items()
    ):
        # Ответ сессии — ответ последнего хода: второй копией в запись не
        # идёт (иначе два ответа по ~600 КБ не помещались бы вместе).
        bounded = dict.fromkeys(_ANSWER_FIELDS)
        resume = replace(resume, answer_in_outcome=True)
    else:
        bounded = _bounded_outcome(current)
        resume = replace(
            resume,
            answer_truncated=bool(bounded.pop("answer_truncated", False))
            or resume.answer_truncated,
        )
    resume = replace(
        resume,
        **bounded,
        turn_outcomes={
            turn: _bounded_outcome(
                outcome,
                LATEST_ANSWER_KEEP_BYTES
                if turn == latest
                else ANSWER_KEEP_BYTES,
            )
            for turn, outcome in resume.turn_outcomes.items()
        },
    )
    if payload_size(asdict(resume)) <= RESUME_BUDGET:
        return resume
    resume = replace(
        resume,
        undelivered=[
            {"message_id": value.get("message_id")}
            for value in resume.undelivered
        ],
    )
    outcomes = dict(resume.turn_outcomes)
    replies = dict(resume.human_replies)
    latest_cut = False
    while payload_size(asdict(resume)) > RESUME_BUDGET:
        if len(outcomes) > 1:
            outcomes.pop(min(outcomes, key=int))
        elif replies:
            replies.pop(next(iter(replies)))
        elif latest in outcomes and not latest_cut:
            outcomes[latest] = _bounded_outcome(outcomes[latest])
            latest_cut = True
        else:
            break
        resume = replace(resume, turn_outcomes=outcomes, human_replies=replies)
    return resume


def _recent(values):
    """Последние ``DEDUP_WINDOW`` записей: состояние доставки не растёт вечно."""
    if isinstance(values, dict):
        return dict(list(values.items())[-DEDUP_WINDOW:])
    return list(values)[-DEDUP_WINDOW:]


def _with_default_queues(start):
    """Без явных очередей activities идут в очередь самого workflow."""
    if start.resource_queue is not None and start.model_queue is not None:
        return start
    queue = start.resource_queue or workflow.info().task_queue
    return replace(
        start,
        resource_queue=queue,
        model_queue=start.model_queue or queue,
    )


# Без failure_exception_types=[Exception]: недетерминизм и сбой кода
# workflow роняют только workflow task (ход продолжится на исправленном
# воркере); доменные отказы переводит в ApplicationError ``session_failure``.
# Worker Versioning (versioning.enabled у воркера): ход закреплён за сборкой,
# где начался (default_versioning_behavior=PINNED у воркера); Continue-As-New
# переходит на текущую сборку.
@workflow.defn(name="UniversalAgentWorkflow")
class UniversalAgentWorkflow:
    def __init__(self):
        self.start = None
        self.active_turn_id = None
        self.executor = None
        self.children = None
        self.operations = None
        self.phase = "starting"
        self.sequence = 0
        self.run_identity = ""
        self.pending_human = None
        self.human_replies = {}
        self.reply_receipts = {}
        self.stop_requested = False
        self.pending_messages = []
        self.undelivered = []
        self.accepted = {}
        self.accepting_inputs = False
        self.closed = False
        self.ready = False
        self.turns = 0
        #: ID сообщения → номер хода, который его обработал.
        self.completed_inputs = {}
        #: ID сообщений, отданных идущему ходу.
        self.turn_inputs = []
        #: Итоги последних ходов по номеру (ответ хода сообщения, а не
        #: последнего): ``{"<номер>": {"answer", "terminal_outcome", …}}``.
        self.turn_outcomes = {}
        self.answer = None
        self.formatted_answer = None
        self.post_error = None
        self.terminal_outcome = None
        self.resumed_store = None
        #: Прокси треда с подтверждённым владением (после claim или resume).
        self.store = None
        self.owner_released = False
        #: Continue-As-New не уместился в запись истории: сессия живёт в этом
        #: run (новая сборка не дёргает его снова).
        self.continue_blocked = False
        #: Отказ последнего хода (значение больше записи истории).
        self.turn_error = None
        #: Ответ сессии укорочен при переносе через Continue-As-New.
        self.answer_truncated = False
        #: Отпечаток настроек из сообщений: сверяется воркером в начале хода.
        self.message_fingerprint = None
        #: Уровень activities.thread_store; до подготовки хода — умолчания.
        self.store_settings = None

    def new_id(self, prefix):
        self.sequence += 1
        digest = hashlib.sha256(
            f"{self.run_identity}/{prefix}/{self.sequence}".encode()
        ).hexdigest()
        return f"{prefix}_{digest}"

    @workflow.query
    def status(self) -> dict:
        state = self.executor.get_state() if self.executor else None
        return {
            "phase": self.phase,
            "turn_id": self.active_turn_id,
            "system_id": self.start.system_id if self.start else None,
            "thread_id": self.start.thread_id if self.start else None,
            "run_id": self.run_identity,
            "owner": self.operations.owner_id if self.operations else None,
            "active_child": self.children.active_child
            if self.children
            else None,
            "pending_human": self.pending_human,
            "child": {
                "workflow_id": self.children.active_child,
                "pending_human": self.pending_human,
            }
            if self.children and self.children.active_child
            else None,
            "stop_requested": self.stop_requested,
            "steps": state.steps if state else 0,
            "tool_calls": state.tool_calls if state else 0,
            "turns": self.turns,
            "answer": self.answer,
            "formatted_answer": self.formatted_answer,
            "post_error": self.post_error,
            # Ответ пережил Continue-As-New укороченным (полный — в треде).
            "answer_truncated": self.answer_truncated,
            "mailbox_size": len(self.pending_messages),
            "undelivered": self.undelivered,
        }

    @workflow.signal
    async def child_human(self, payload: dict):
        if self.children is not None:
            try:
                await self.children.answer_child_human(payload)
            except Exception:
                # Ответ мог не дойти до уже завершившегося ребёнка.
                workflow.logger.warning("Human answer to the child failed")

    @workflow.signal
    def child_checkpoint(self, payload: dict):
        if self.children is not None:
            self.children.receive_checkpoint(payload)

    @workflow.signal
    async def stop(self):
        if self.phase in {"idle", "stopped"} and not self.accepting_inputs:
            return
        self.stop_requested = True
        if self.executor is not None:
            self.executor.stop_run()
        stop_model = getattr(self.operations, "stop_model", None)
        if stop_model is not None:
            # Не ждать конца повторов запроса к модели (сбой провайдера
            # Temporal повторяет без ограничения).
            stop_model()
        stop_child = getattr(self.operations, "stop_child", None)
        if stop_child is not None:
            try:
                await stop_child()
            except Exception:
                # Ребёнок мог завершиться раньше сигнала. Исключение в
                # обработчике сигнала повторяло бы workflow task вечно.
                workflow.logger.warning("Stop signal to the child failed")

    @workflow.update
    async def stop_turn(self, turn_id: str) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        if turn_id != self.active_turn_id:
            raise ApplicationError("Turn is stale", non_retryable=True)
        await self.stop()
        return {"turn_id": turn_id, "stop_requested": self.stop_requested}

    @workflow.signal
    def close_session(self):
        """Finish accepted work, then close; this does not abort a tool."""
        self.closed = True

    @workflow.update
    async def submit_message(self, message: UserMessage) -> dict:
        await workflow.wait_condition(lambda: self.ready)
        return self._accept(message)

    @workflow.update
    async def submit_batch(self, messages: list[UserMessage]) -> list[dict]:
        """Несколько сообщений одним update — в один ход, как список
        ``AsyncAgent.run([...])`` локально (по одному update ход мог начаться
        до прихода остальных)."""
        await workflow.wait_condition(lambda: self.ready)
        # Атомарно: всё, что может отказать, — до приёма первого сообщения,
        # иначе отказ на третьем оставил бы первые два принятыми.
        seen = {}
        for message in messages:
            digest = _message_digest(message.text)
            if seen.setdefault(message.message_id, digest) != digest:
                raise ApplicationError(_MESSAGE_ID_REUSED, non_retryable=True)
            self._precheck(message, digest)
        return [self._accept(message) for message in messages]

    @submit_batch.validator
    def _validate_submit_batch(self, messages: list[UserMessage]) -> None:
        if not messages:
            raise ValueError("Empty message batch")
        for message in messages:
            message.validate()

    def _check_fingerprint(self, fingerprint):
        """Настройки отправителя против уже известных сессии: расходятся —
        отказ этого сообщения сразу (сессия работает дальше по своим);
        сессия их ещё не знает — сверит воркер в начале хода."""
        if not fingerprint:
            return
        known = self.message_fingerprint or self.start.settings_fingerprint
        mismatch = fingerprint_mismatch(fingerprint, known) if known else []
        if mismatch:
            raise ApplicationError(
                "Настройки приложения расходятся с настройками сессии в "
                f"секциях: {', '.join(mismatch)}",
                type="SettingsMismatch",
                non_retryable=True,
            )
        self.message_fingerprint = fingerprint

    def _precheck(self, message, digest):
        """Отказы приёма без изменения состояния (``submit_batch`` проверяет
        весь пакет до приёма)."""
        existing = self.accepted.get(message.message_id)
        if existing is not None:
            if existing != digest:
                raise ApplicationError(_MESSAGE_ID_REUSED, non_retryable=True)
            return  # повтор принятого — ``_accept`` ответит duplicate
        if self.closed:
            raise ApplicationError("Session is closing", non_retryable=True)
        if message.settings_fingerprint:
            known = self.message_fingerprint or self.start.settings_fingerprint
            if known and fingerprint_mismatch(
                message.settings_fingerprint, known
            ):
                self._check_fingerprint(message.settings_fingerprint)

    def _accept(self, message):
        """Принять сообщение: дедупликация по ID, в идущий ход или в
        очередь следующего."""
        digest = _message_digest(message.text)
        existing = self.accepted.get(message.message_id)
        if existing is not None:
            if existing != digest:
                raise ApplicationError(_MESSAGE_ID_REUSED, non_retryable=True)
            if not any(
                value.get("message_id") == message.message_id
                for value in self.undelivered
            ):
                return {"message_id": message.message_id, "duplicate": True}
            # Повтор сообщения, отброшенного остановкой хода: принять заново.
        if self.closed:
            raise ApplicationError("Session is closing", non_retryable=True)
        self._check_fingerprint(message.settings_fingerprint)
        self._take_undelivered(message.message_id)
        self.accepted[message.message_id] = digest
        value = {
            "role": "user",
            "content": message.text,
            "message_id": message.message_id,
        }
        if self.accepting_inputs:
            self.executor.add_message(value)
            self.turn_inputs.append(message.message_id)
        else:
            self.pending_messages.append(value)
        return {"message_id": message.message_id, "duplicate": False}

    @workflow.query
    def message_state(self, message_id: str) -> dict:
        """Состояние сообщения для ожидающего клиента (опрос, не update).

        Ожидание — query: долгие update занимали бы серверный лимит
        одновременных update (по умолчанию 10), и ответ человека, stop и
        новые сообщения упирались бы в него. ``state``: ``pending`` (ещё не
        обработано), ``done`` (ход с номером ``turn`` закончился; его ответ —
        если ход среди последних хранимых, иначе ``answer_expired``),
        ``dropped`` (отброшено остановкой хода), ``unknown`` (не принято или
        старше последних 1000 ID).
        """
        if message_id not in self.accepted:
            return {"message_id": message_id, "state": "unknown"}
        turn = self.completed_inputs.get(message_id)
        if turn is None:
            dropped = any(
                value.get("message_id") == message_id
                for value in self.undelivered
            )
            return {
                "message_id": message_id,
                "state": "dropped" if dropped else "pending",
            }
        outcome = self.turn_outcomes.get(str(turn))
        if outcome is not None:
            # Тот же вид, что переживёт Continue-As-New: ответ не зависит от
            # того, успел ли клиент спросить до нового run.
            latest = str(turn) == max(self.turn_outcomes, key=int)
            outcome = _bounded_outcome(
                outcome,
                LATEST_ANSWER_KEEP_BYTES if latest else ANSWER_KEEP_BYTES,
            )
        return {
            "message_id": message_id,
            "state": "done",
            "turn": turn,
            "turns": self.turns,
            "answer_expired": outcome is None,
            **(outcome or {}),
        }

    def _take_undelivered(self, message_id):
        """Убрать сообщение из отброшенных остановкой (повтор принят заново)."""
        self.undelivered = [
            value
            for value in self.undelivered
            if value.get("message_id") != message_id
        ]

    @submit_message.validator
    def _validate_submit_message(self, message: UserMessage) -> None:
        # Отказ до приёма update: исключение после приёма (не ApplicationError)
        # роняло бы workflow task, а не update.
        message.validate()

    @workflow.update
    async def answer_human(
        self, request_id: str, answer: str, reply_id: str | None = None
    ) -> bool:
        await workflow.wait_condition(lambda: self.ready)
        if reply_id in self.reply_receipts:
            if self.reply_receipts[reply_id] != [request_id, answer]:
                raise ApplicationError("Reply ID reused", non_retryable=True)
        else:
            self._answer_human(request_id, answer)
            if reply_id is not None:
                self.reply_receipts[reply_id] = [request_id, answer]
        return True

    def _answer_human(self, request_id, answer) -> None:
        existing = self.human_replies.get(request_id)
        if existing is not None:
            if existing != answer:
                raise ApplicationError(
                    "Human reply changed", non_retryable=True
                )
            return
        if (
            not self.pending_human
            or self.pending_human["request_id"] != request_id
            or self.stop_requested
        ):
            raise ApplicationError(
                "Human request is stale", non_retryable=True
            )
        if self.pending_human["kind"] == "confirm" and answer not in {
            CONFIRM_ALLOW_ONCE,
            CONFIRM_ALLOW_REMEMBER,
            CONFIRM_DENY_ONCE,
            CONFIRM_DENY_REMEMBER,
        }:
            raise ApplicationError(
                "Unknown confirmation choice", non_retryable=True
            )
        self.human_replies[request_id] = answer

    async def human_request(self, request, *, flush=True, abandon=None):
        prepared = getattr(self.operations, "prepared", None)
        if prepared is not None and not prepared.human_input:
            # Человека в этом развёртывании нет (``human_input: false``):
            # отказ сразу — подтверждение не дано, вопрос без ответа, — а не
            # вечное ожидание ответа, которого не будет.
            workflow.logger.warning(
                "Human request %s refused: human_input is off", request.kind
            )
            raise HumanRequestInterrupted(
                "No human answers in this deployment (human_input: false)"
            )
        # Ожидание может длиться часами: шаги и сам вопрос — в тред до него.
        # Вопрос ребёнка приходит сигналом, параллельно с основным циклом:
        # там тред родителя не трогаем, он запишется в конце его хода.
        if flush and self.executor is not None:
            await self.executor.aflush_thread()
        request_id = self.new_id("human")
        self.pending_human = {**asdict(request), "request_id": request_id}
        self.phase = "waiting_human"
        try:
            await workflow.wait_condition(
                lambda: (
                    self.stop_requested
                    or request_id in self.human_replies
                    or (abandon is not None and abandon())
                )
            )
            if self.stop_requested:
                raise HumanRequestInterrupted(
                    "Agent was stopped while waiting for a human"
                )
            if request_id not in self.human_replies:
                raise HumanRequestInterrupted("Human request was abandoned")
            return self.human_replies[request_id]
        finally:
            self.pending_human = None
            self.phase = "running"

    def _initialize(self, start):
        self.start = start
        if start.format_text is not None and not start.format_text.strip():
            raise ValueError("Structured output format must be nonempty")
        if start.schema_version != 1:
            raise ValueError("Invalid run request")
        if start.resume is not None:
            self._restore_delivery(start.resume)
        else:
            self._initial_messages(start)
        self.phase = "idle"
        self.ready = True

    def _restore_delivery(self, resume):
        if resume.schema_version != 1:
            raise ValueError("Unsupported session resume")
        self.closed = self.closed or resume.closed
        if resume.store_owner_epoch is not None:
            # Эпоха аренды треда: run после Continue-As-New пишет по ней
            # без нового захвата.
            self.resumed_store = resume.store_owner_epoch
        self.human_replies = dict(resume.human_replies)
        self.reply_receipts = dict(resume.reply_receipts)
        self.undelivered = list(resume.undelivered)
        self.accepted = dict(resume.accepted)
        self.pending_messages = list(resume.pending)
        self.turns = resume.turns
        self.completed_inputs = dict(resume.completed)
        self.turn_outcomes = dict(resume.turn_outcomes)
        self.message_fingerprint = resume.message_fingerprint
        last = (
            self.turn_outcomes.get(max(self.turn_outcomes, key=int))
            if resume.answer_in_outcome and self.turn_outcomes
            else None
        )
        source = last if last is not None else asdict(resume)
        self.answer = source.get("answer")
        self.formatted_answer = source.get("formatted_answer")
        self.post_error = source.get("post_error")
        self.answer_truncated = bool(source.get("answer_truncated"))
        self.terminal_outcome = resume.terminal_outcome

    def _initial_messages(self, start):
        if not start.messages:
            if start.keep_alive:
                # Сессия, открытая Update-With-Start: ввод придёт update'ом.
                return
            raise ValueError("Run requires messages")
        ids = start.message_ids or [self.new_id("m") for _ in start.messages]
        if len(ids) != len(start.messages) or len(set(ids)) != len(ids):
            raise ValueError("Message IDs must match messages and be unique")
        for message_id, text in zip(ids, start.messages):
            message = UserMessage(message_id, text)
            message.validate()
            self.accepted[message_id] = _message_digest(text)
            self.pending_messages.append(
                {"role": "user", "content": text, "message_id": message_id}
            )

    async def _prepare_turn(self, start, owner, store):
        self.phase = "starting"
        self.active_turn_id = (
            f"{workflow.info().workflow_id}/turn-{self.turns + 1}"
        )
        # Владелец контекста — ход цепочки run: новый run на том же
        # workflow ID не наследует контекст и ресурсы прошлой цепочки.
        chain = workflow.info().first_execution_run_id
        self.operations = WorkflowOperations(
            start, f"{owner}/{chain}/turn-{self.turns + 1}", self.new_id
        )
        prepared = await self.operations.call(
            "universal_agent.context.prepare",
            {
                "store_id": start.store_id,
                "settings_fingerprint": self.message_fingerprint
                or start.settings_fingerprint,
            },
            PreparedContext,
        )
        self.operations.resource_owner = prepared.resource_owner
        self.operations.tool_metadata = prepared.tool_metadata
        self.operations.prepared = prepared
        if prepared.activity_settings:
            self.operations.activity_settings = prepared.activity_settings
        self.store_settings = self.operations.activity_settings["thread_store"]
        if store is None:
            store = self._thread_store(start, owner)
            key = _thread_key(start)
            if self.resumed_store is not None:
                store.adopt(key, Lease(owner=owner, epoch=self.resumed_store))
            else:
                # Тред сессии — за ней, бессрочно; у владельца, чей workflow
                # уже не исполняется, воркер его отбирает.
                await store.claim(key, LeaseRequest(owner=owner, ttl=None))
        self.store = store
        # Сообщения, прочитанные из треда, идут в activity модели ссылками.
        self.operations.thread_store = store
        ctx = assemble_context(
            prepared,
            operations=self.operations,
            store=store,
            logger=workflow.logger,
            human_request=self.human_request,
        )
        self.children = install_subagents(
            ctx,
            start,
            prepared,
            store,
            self.operations,
            human_request=lambda request, **kwargs: self.human_request(
                request, flush=False, **kwargs
            ),
        )
        self.ctx = ctx
        self.executor = AgentExecutor(ctx)
        self.operations.flush_thread = self.executor.aflush_thread
        self.children.redeliver = self.executor.add_message
        return store

    async def _run_turn(self, start):
        messages, self.pending_messages = self.pending_messages, []
        self.turn_inputs = [value["message_id"] for value in messages]
        if self.stop_requested:
            self.executor.stop_run()
        self.phase = "running"
        self.accepting_inputs = True
        ran = False
        self.formatted_answer = self.post_error = self.turn_error = None
        self.answer_truncated = False
        try:
            try:
                self.answer = await self.executor.arun(
                    [value["content"] for value in messages],
                    thread_id=start.thread_id,
                    system=start.system_id,
                    message_ids=[value["message_id"] for value in messages],
                    account_run=start.format_text is None,
                )
            except ModelRequestStopped:
                # stop отменил запрос к модели вне шага цикла (сжатие
                # контекста и т.п.) — ход остановлен, не упал.
                self.answer = None
            except HistoryLimitExceeded as exc:
                # Значение не легло в запись истории: отказ хода, не сессии
                # (следующий ход — новый run после Continue-As-New).
                workflow.logger.warning("Turn failed: %s", exc)
                self.answer, self.turn_error = None, str(exc)
            ran = self.turn_error is None
            if ran and start.format_text is not None:
                self.phase = "postprocessing"
                try:
                    self.formatted_answer = await arephrase_turn_outcome(
                        format_text=start.format_text,
                        response=self.answer,
                        ctx=self.ctx,
                        state=self.executor.get_state(),
                        terminal_outcome=self.executor.last_terminal_outcome,
                    )
                except StructuredOutputError as exc:
                    self.post_error = str(exc)
                except ModelRequestStopped:
                    # Повторный stop прервал постобработку.
                    self.post_error = "Stopped"
        finally:
            try:
                # stop/ошибка: отложенные шаги не должны остаться только в
                # истории workflow.
                try:
                    await self.executor.aflush_thread()
                except HistoryLimitExceeded as exc:
                    workflow.logger.warning("Turn flush failed: %s", exc)
                    self.turn_error = self.turn_error or str(exc)
                if ran and start.format_text is not None:
                    self.phase = "accounting"
                    await self.executor.aclose_run_accounting()
            finally:
                self.accepting_inputs = False
                self.pending_messages.extend(
                    self.executor.take_undelivered_messages()
                )
        if self.stop_requested:
            self.undelivered.extend(self.pending_messages)
            self.pending_messages = []
        await self.operations.release_context()
        self._finish_turn(self.executor.last_terminal_outcome)

    async def _fail_unprepared_turn(self, exc):
        """Подготовка хода не легла в запись истории (промпт, AGENTS.md из
        хранилища агента больше лимита): отказ хода, а не сессии — сообщения
        хода получают ``turn_error``, сессия принимает следующие."""
        workflow.logger.warning("Turn failed before start: %s", exc)
        messages, self.pending_messages = self.pending_messages, []
        self.turn_inputs = [value["message_id"] for value in messages]
        self.answer = self.formatted_answer = self.post_error = None
        self.turn_error = str(exc)
        self.answer_truncated = False
        try:
            await self.operations.release_context()
        except Exception:
            workflow.logger.exception("Context release failed")
        self._finish_turn(None)

    def _finish_turn(self, terminal_outcome):
        """Учёт законченного хода: номер, обработанные сообщения, итог."""
        self.turns += 1
        # Обработаны ходом — отданные ему и не вернувшиеся из него
        # (опоздавшее к последнему забору ввода уходит в следующий ход).
        later = {value.get("message_id") for value in self.pending_messages}
        later.update(value.get("message_id") for value in self.undelivered)
        for message_id in self.turn_inputs:
            if message_id not in later:
                self.completed_inputs[message_id] = self.turns
        self.turn_inputs = []
        self.turn_outcomes[str(self.turns)] = {
            "answer": self.answer,
            "terminal_outcome": terminal_outcome,
            "formatted_answer": self.formatted_answer,
            "post_error": self.post_error,
            "turn_error": self.turn_error,
        }
        # Последние ходы: ответы бывают большими, а состояние едет в
        # Continue-As-New (запись истории).
        for old in sorted(self.turn_outcomes, key=int)[:-TURN_OUTCOMES_KEPT]:
            del self.turn_outcomes[old]
        # Итоги ходов — сразу в том виде, что переживёт Continue-As-New:
        # ``wait`` до и после нового run видит один и тот же ответ.
        self.turn_outcomes = dict(
            _fit_resume(self._session_resume(None)).turn_outcomes
        )
        self.terminal_outcome = terminal_outcome
        self.phase = "stopped" if self.stop_requested else "idle"

    @workflow.run
    async def run(self, start: RunRequest) -> dict:
        self.run_identity = workflow.info().run_id
        start = _with_default_queues(start)
        self.start = start
        self._initialize_checked(start)
        with execution_environment(new_id=self.new_id, now=workflow.now):
            try:
                result = await self._run_session(start)
                cancellation = workflow.cancellation_reason()
                if cancellation is not None:
                    raise asyncio.CancelledError(cancellation)
                return result
            except (Exception, asyncio.CancelledError) as exc:
                cancelled = isinstance(exc, asyncio.CancelledError)
                failure = None if cancelled else session_failure(exc)
                if failure is None and not cancelled:
                    # Сбой кода workflow: без новых команд, только сбой
                    # workflow task. Уборка здесь попала бы в историю, и
                    # replay исправленного кода с ней разошёлся бы.
                    raise
                await self._abort_session(start)
                if failure is None or failure is exc:
                    raise
                raise failure from exc

    def _initialize_checked(self, start):
        """``_initialize`` с отказом некорректного запроса без повторов."""
        try:
            self._initialize(start)
        except ValueError as exc:
            raise ApplicationError(
                str(exc), type="ValueError", non_retryable=True
            ) from exc

    async def _abort_session(self, start):
        """Уборка упавшей или отменённой сессии: контекст и владение тредом."""
        self.closed = True
        self.accepting_inputs = False
        self.phase = "failed"
        if self.operations is not None:
            try:
                await asyncio.shield(self.operations.mcp_cleanup())
                await asyncio.shield(self.operations.release_context())
            except (Exception, asyncio.CancelledError):
                workflow.logger.exception("Context cleanup failed")
        try:
            # Упавшая сессия не держит тред: иначе он закрыт для
            # локального TUI/CLI и для workflow с другим ID.
            await asyncio.shield(self._release_owner(start))
        except (Exception, asyncio.CancelledError):
            workflow.logger.exception("Thread owner release failed")

    async def _run_session(self, start):
        # Владелец треда — сессия целиком: Continue-As-New его не меняет.
        owner = workflow.info().workflow_id
        while True:
            ran = await self._serve_pending(start, owner)
            if not start.keep_alive or (
                self.closed and not self.pending_messages
            ):
                break
            self.stop_requested = False
            if ran and not self.closed and not self.pending_messages:
                # Ход закончен: новый run начинается с почти пустой историей.
                await self._continue(start, self.store)
            await self._wait_idle(start)
        self.closed = True
        await workflow.wait_condition(workflow.all_handlers_finished)
        await self._release_owner(start)
        self.phase = "stopped" if self.stop_requested else "completed"
        return self._session_result()

    async def _serve_pending(self, start, owner):
        """Ход по накопленному вводу; ``True`` — ход был."""
        if not self.pending_messages:
            return False
        try:
            await self._prepare_turn(start, owner, self.store)
        except HistoryLimitExceeded as exc:
            await self._fail_unprepared_turn(exc)
        else:
            await self._run_turn(start)
        return True

    def _idle_wakeup(self):
        """Повод простаивающей сессии проснуться: ввод, закрытие или новая
        сборка воркера (если Continue-As-New не заблокирован)."""
        return (
            bool(self.pending_messages)
            or self.closed
            or (_new_build_announced() and not self.continue_blocked)
        )

    async def _wait_idle(self, start):
        """Ожидание ввода простаивающей сессией (с Continue-As-New)."""
        try:
            await workflow.wait_condition(
                self._idle_wakeup, timeout=IDLE_REFRESH
            )
        except TimeoutError:
            # Долго простаивающая сессия раз в сутки начинает новый run:
            # переходит на текущую сборку воркера (versioning) и не
            # держит открытым один run неделями.
            await self._continue(start, self.store)
        else:
            if not self.pending_messages and not self.closed:
                # Объявлена новая сборка: простаивающая сессия переходит
                # на неё сразу, старый воркер не ждёт её сутки.
                await self._continue(start, self.store)

    def _session_result(self):
        """Результат workflow — тоже запись истории: ответ — в пределах
        (полный — в треде), отброшенные сообщения — без текста, если много."""
        failed = self.post_error or self.terminal_outcome or self.turn_error
        result = {
            **_bounded_outcome(
                {
                    "answer": self.answer,
                    "formatted_answer": self.formatted_answer,
                    "post_error": self.post_error,
                }
            ),
            "undelivered": [*self.undelivered, *self.pending_messages],
            "terminal_outcome": self.terminal_outcome,
            "turns": self.turns,
            "turn_error": self.turn_error,
            "exit_code": 1 if failed else 0,
        }
        if self.answer_truncated:
            result["answer_truncated"] = True
        if payload_size(result) > RESUME_BUDGET:
            result["undelivered"] = [
                {"message_id": value.get("message_id")}
                for value in result["undelivered"]
            ]
        return result

    def _thread_store(self, start, owner):
        return TemporalThreadStore(
            start.store_id,
            ThreadStoreTransport(
                task_queue=start.resource_queue,
                settings=self.store_settings,
            ),
            owner=owner,
        )

    async def _release_owner(self, start):
        """Снять аренду треда, если эта цепочка run её держит.

        Run после Continue-As-New, закрытый без хода, владеет тредом по
        resume: прокси собирается с сохранённой эпохой.
        """
        if self.owner_released:
            return
        store, key = self.store, _thread_key(start)
        if store is None and self.resumed_store is not None:
            owner = workflow.info().workflow_id
            store = self._thread_store(start, owner)
            store.adopt(key, Lease(owner=owner, epoch=self.resumed_store))
        if store is None or store.lease is None:
            return
        await store.release(key, store.lease)
        self.owner_released = True

    def _session_resume(self, store):
        """Состояние сессии для Continue-As-New (до ``_fit_resume``)."""
        # Run из CAN без хода владеет тредом по resume: передать его дальше,
        # иначе суточный CAN простаивающей сессии терял бы владение.
        owner_epoch = self.resumed_store
        if store is not None and store.lease is not None:
            owner_epoch = store.lease.epoch
        return SessionResume(
            human_replies=_recent(self.human_replies),
            reply_receipts=_recent(self.reply_receipts),
            formatted_answer=self.formatted_answer,
            post_error=self.post_error,
            closed=self.closed,
            undelivered=_recent(self.undelivered),
            accepted=_recent(self.accepted),
            pending=self.pending_messages,
            turns=self.turns,
            completed=_recent(self.completed_inputs),
            turn_outcomes=self.turn_outcomes,
            message_fingerprint=self.message_fingerprint,
            answer=self.answer,
            terminal_outcome=self.terminal_outcome,
            store_owner_epoch=owner_epoch,
        )

    async def _continue(self, start, store):
        self.phase = "continuing"
        await workflow.wait_condition(workflow.all_handlers_finished)
        if self.pending_messages or self.closed:
            # Пока ждали обработчики, пришёл ввод: сначала обслужить его.
            self.phase = "idle"
            return
        resume = _fit_resume(self._session_resume(store))
        size = payload_size(asdict(resume))
        if size > ACTIVITY_INPUT_LIMIT:
            # Не уменьшить (принятый ввод не выбросить): сессия продолжается
            # в этом run, без Continue-As-New, — не вечный сбой задачи.
            workflow.logger.warning(
                "Continue-As-New skipped: session state is %s bytes", size
            )
            self.continue_blocked = True
            self.phase = "idle"
            return
        # Без await между проверкой и CAN: весь принятый ввод уже в resume.
        workflow.continue_as_new(
            replace(start, messages=[], message_ids=None, resume=resume),
            # Следующий ход — на текущей сборке; без versioning не задаётся
            # (сервер отвергает поведение у неверсионированного воркера).
            initial_versioning_behavior=(
                workflow.ContinueAsNewVersioningBehavior.AUTO_UPGRADE
                if workflow.info().get_current_deployment_version() is not None
                else None
            ),
        )


def _thread_key(start) -> ThreadKey:
    return ThreadKey(system=start.system_id, thread=start.thread_id)
