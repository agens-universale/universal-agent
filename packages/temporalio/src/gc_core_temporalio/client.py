"""Клиент сессий агента в Temporal: ``AgentClient`` (низкий уровень),
``AgentSession`` (тред: отправить и дождаться), ``TemporalDurability``
(``AsyncAgent`` через Temporal) и CLI ``gc-temporal``."""

import argparse
import asyncio
import contextlib
import hashlib
import json
import logging
import uuid

from gc_core.execution.contracts import ExecutionFailure
from gc_core.structured_output import read_format_description
from temporalio.client import Client, WithStartWorkflowOperation
from temporalio.common import WorkflowIDConflictPolicy, WorkflowIDReusePolicy

from gc_core_temporalio.contracts import RunRequest, UserMessage
from gc_core_temporalio.public_config import settings_fingerprint
from gc_core_temporalio.workflow import UniversalAgentWorkflow

logger = logging.getLogger(__name__)


class AgentClient:
    def __init__(self, client, *, workflow_queue="ua-workflow"):
        self.client, self.workflow_queue = client, workflow_queue

    async def start(
        self,
        request: RunRequest,
        *,
        workflow_id=None,
        retry_policy=None,
        execution_timeout=None,
    ):
        """Старт сессии.

        ``retry_policy`` и ``execution_timeout`` — параметры Temporal для всего
        workflow, по умолчанию не заданы. Повтор workflow начинает ход заново
        с тем же входом, и уже выполненные эффекты инструментов повторятся —
        сбои отдельных шагов повторяются activities (секция ``activities``
        конфига воркера).
        """
        if request.format_text is not None and not request.format_text.strip():
            raise ValueError("Structured output format must be nonempty")
        if not request.messages or not all(
            isinstance(text, str) and text.strip() for text in request.messages
        ):
            raise ValueError("At least one nonempty message is required")
        return await self.client.start_workflow(
            UniversalAgentWorkflow.run,
            request,
            id=workflow_id or f"ua-session-{uuid.uuid4().hex}",
            task_queue=self.workflow_queue,
            retry_policy=retry_policy,
            execution_timeout=execution_timeout,
            id_reuse_policy=WorkflowIDReusePolicy.REJECT_DUPLICATE,
        )

    async def status(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).query(
            UniversalAgentWorkflow.status
        )

    async def result(self, workflow_id):
        return await self.client.get_workflow_handle(workflow_id).result()

    async def stop(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            UniversalAgentWorkflow.stop
        )

    async def stop_turn(self, workflow_id, turn_id):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(UniversalAgentWorkflow.stop_turn, turn_id)

    async def close_session(self, workflow_id):
        await self.client.get_workflow_handle(workflow_id).signal(
            UniversalAgentWorkflow.close_session
        )

    async def submit_with_start(
        self, workflow_id, request: RunRequest, message: UserMessage
    ):
        """Отправить сообщение, открыв сессию при необходимости, — атомарно.

        Закрытая или упавшая сессия с тем же ID начинается заново новым run.
        """
        message.validate()
        operation = WithStartWorkflowOperation(
            UniversalAgentWorkflow.run,
            request,
            id=workflow_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            task_queue=self.workflow_queue,
        )
        return await self.client.execute_update_with_start_workflow(
            UniversalAgentWorkflow.submit_message,
            message,
            start_workflow_operation=operation,
        )

    async def submit_batch_with_start(
        self, workflow_id, request: RunRequest, messages: list[UserMessage]
    ):
        """Несколько сообщений одним update (в один ход), открыв сессию при
        необходимости, — атомарно."""
        for message in messages:
            message.validate()
        operation = WithStartWorkflowOperation(
            UniversalAgentWorkflow.run,
            request,
            id=workflow_id,
            id_conflict_policy=WorkflowIDConflictPolicy.USE_EXISTING,
            id_reuse_policy=WorkflowIDReusePolicy.ALLOW_DUPLICATE,
            task_queue=self.workflow_queue,
        )
        return await self.client.execute_update_with_start_workflow(
            UniversalAgentWorkflow.submit_batch,
            messages,
            start_workflow_operation=operation,
        )

    async def submit_message(self, workflow_id, message: UserMessage):
        message.validate()
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            UniversalAgentWorkflow.submit_message,
            message,
        )

    async def answer_human(
        self, workflow_id, request_id, answer, *, reply_id=None
    ):
        return await self.client.get_workflow_handle(
            workflow_id
        ).execute_update(
            UniversalAgentWorkflow.answer_human,
            args=[request_id, answer, reply_id],
        )


class SessionFailed(RuntimeError):
    """Сессия агента упала (отказ хода, расхождение настроек приложения и
    воркера); текст — исходная причина. Следующее сообщение в тот же тред
    откроет новый run."""


class SessionStuck(SessionFailed):
    """Задача workflow сессии падает раз за разом (сбой кода, недетерминизм):
    workflow жив, но ход не идёт. Ожидание прекращено; сессия продолжится
    сама, когда сбой устранят."""


class SettingsMismatch(ValueError):
    """Настройки приложения расходятся с настройками идущей сессии:
    сообщение не принято (сессия работает дальше по своим настройкам)."""


@contextlib.contextmanager
def _settings_mismatch():
    from temporalio.client import WorkflowUpdateFailedError

    try:
        yield
    except WorkflowUpdateFailedError as exc:
        cause = exc.cause
        if getattr(cause, "type", None) == "SettingsMismatch":
            raise SettingsMismatch(cause.message) from exc
        raise


class TurnFailed(ExecutionFailure):
    """Ход отказал (значение не легло в запись истории Temporal), сессия
    жива и принимает следующие сообщения. Как ``ExecutionFailure`` хода в
    локальном режиме."""


class AnswerTruncated(RuntimeError):
    """Ответ хода больше, чем переживает Continue-As-New (~1 МБ в истории):
    ``answer`` — его начало, полный — последним сообщением в треде."""

    def __init__(self, message, answer=None):
        super().__init__(message)
        self.answer = answer


def _raise_turn_error(reply):
    if reply.get("turn_error"):
        raise TurnFailed(reply["turn_error"])
    if reply.get("answer_truncated"):
        raise AnswerTruncated(
            f"Ответ хода {reply.get('turn')} укорочен для истории Temporal; "
            "полный — последним сообщением в треде",
            reply.get("answer"),
        )


#: ``wait``: столько попыток задачи workflow подряд — сессия «застряла».
STUCK_TASK_ATTEMPTS = 3
#: ``wait``: состояние workflow (``describe``) — раз в столько опросов.
DESCRIBE_EVERY = 5


class MessageNotProcessed(RuntimeError):
    """Сообщение отброшено остановкой хода до того, как ход его взял;
    повтор с тем же ID принимается заново."""

    def __init__(self, message_id):
        super().__init__(
            f"Message {message_id} was dropped by a stopped turn; resend it"
        )
        self.message_id = message_id


def session_workflow_id(agent, store_id, system_id, thread_id):
    """ID workflow сессии: агент и тред. Один тред одного агента — одна
    сессия у любого клиента; другой агент на том же треде — другая сессия,
    и она упрётся во владение тредом явной ошибкой, а не молча получит
    чужого агента (с update-with-start ``USE_EXISTING`` чужой запрос на
    старт просто игнорировался бы)."""
    digest = hashlib.sha256(
        json.dumps([agent, store_id, system_id, thread_id]).encode()
    ).hexdigest()[:32]
    return f"ua-session-{digest}"


def _message_reply(state, message_id):
    """Ответ ``wait`` по состоянию сообщения; ``None`` — ждать дальше."""
    if state is None:
        return None
    kind = state["state"]
    if kind == "done":
        return {**state, "delivered": True}
    if kind == "dropped":
        return {**state, "delivered": False, "answer": None}
    if kind == "unknown":
        raise ValueError(f"Unknown message: {message_id}")
    return None


class AgentSession:
    """Сессия агента в Temporal — один тред.

    ``ask`` — «отправить и дождаться ответа хода»; ``send`` — только
    отправить (в идущий ход или в следующий). Сессия живёт между
    сообщениями (``keep_alive``) и переживает перезапуск клиента: тот же
    тред — тот же workflow.
    """

    def __init__(
        self,
        client,
        *,
        agent,
        thread_id,
        task_queue,
        system_id="sdk",
        store_id="default",
        resource_queue=None,
        model_queue=None,
        format_text=None,
        workflow_id=None,
        settings=None,
    ):
        self.client = client
        self.task_queue = task_queue
        #: Явный ``workflow_id`` — подключение к уже идущему workflow (шаг
        #: ``agent_step``): ``send`` его не запускает, иначе под ID шага
        #: родился бы чужой workflow и следующий шаг с тем же именем упал бы.
        self.attached = workflow_id is not None
        self.workflow_id = workflow_id or session_workflow_id(
            agent, store_id, system_id, thread_id
        )
        self.request = RunRequest(
            agent,
            store_id,
            system_id,
            thread_id,
            [],
            resource_queue,
            model_queue,
            format_text=format_text,
            keep_alive=True,
            # Настройки приложения: воркер сверит их со своим профилем.
            settings_fingerprint=settings_fingerprint(settings)
            if settings is not None
            else None,
        )

    @property
    def handle(self):
        # Без run_id: сессия делает Continue-As-New после каждого хода.
        return self.client.get_workflow_handle(self.workflow_id)

    async def send(self, text, *, message_id=None):
        """Отправить сообщение; сессия открывается при необходимости.
        Возвращает ID сообщения (повтор с тем же ID — не второй ход)."""
        message = UserMessage(
            message_id or uuid.uuid4().hex,
            text,
            settings_fingerprint=self.request.settings_fingerprint,
        )
        client = AgentClient(self.client, workflow_queue=self.task_queue)
        with _settings_mismatch():
            if self.attached:
                await client.submit_message(self.workflow_id, message)
            else:
                await client.submit_with_start(
                    self.workflow_id, self.request, message
                )
        return message.message_id

    async def send_batch(self, items):
        """Несколько сообщений ``[(текст, ID | None), …]`` одним update — в
        один ход. Возвращает их ID."""
        messages = [
            UserMessage(
                message_id or uuid.uuid4().hex,
                text,
                settings_fingerprint=self.request.settings_fingerprint,
            )
            for text, message_id in items
        ]
        with _settings_mismatch():
            if self.attached:
                await self.handle.execute_update(
                    UniversalAgentWorkflow.submit_batch, messages
                )
            else:
                await AgentClient(
                    self.client, workflow_queue=self.task_queue
                ).submit_batch_with_start(
                    self.workflow_id, self.request, messages
                )
        return [message.message_id for message in messages]

    async def wait(
        self,
        message_id,
        *,
        poll=0.2,
        max_poll=2.0,
        timeout=None,
        stuck_attempts=STUCK_TASK_ATTEMPTS,
    ):
        """Дождаться хода, обработавшего сообщение: ответ этого хода, его
        номер; ``delivered: False`` — ход остановили до сообщения.

        Опрос query ``message_state`` с растущей паузой: ожидание не занимает
        лимит одновременных update на сервере и не держит workflow (его
        Continue-As-New, закрытие). Обычно один запрос за шаг; состояние
        workflow (``describe``) — при ошибке query, на первом шаге и
        раз в ``DESCRIBE_EVERY`` шагов. Упавшая сессия — ``SessionFailed``; задача
        workflow падает ``stuck_attempts`` раз подряд — ``SessionStuck``;
        ``timeout`` (секунды) истёк — ``TimeoutError``.
        """
        loop = asyncio.get_running_loop()
        deadline = None if timeout is None else loop.time() + timeout
        delay, step = poll, 0
        while True:
            step += 1
            state, failed_query = await self._message_state(message_id)
            reply = _message_reply(state, message_id)
            if reply is not None:
                return reply
            if failed_query or step == 1 or step % DESCRIBE_EVERY == 0:
                reason = await self._failure(
                    pending_message=message_id, stuck_attempts=stuck_attempts
                )
                if reason is not None:
                    raise reason
            if deadline is not None and loop.time() + delay > deadline:
                raise TimeoutError(
                    f"Сообщение {message_id} не обработано за {timeout} с"
                )
            await asyncio.sleep(delay)
            delay = min(delay * 1.5, max_poll)

    async def _message_state(self, message_id):
        """Query ``message_state`` → (состояние | ``None``, query не прошёл)."""
        from temporalio.service import RPCError

        try:
            state = await self.handle.query(
                UniversalAgentWorkflow.message_state, message_id
            )
        except RPCError:
            # Задача workflow в сбое, сессия завершена или ещё стартует.
            return None, True
        return state, False

    async def _failure(self, pending_message=None, stuck_attempts=None):
        """Причина, по которой ответа не будет: сессия упала
        (``SessionFailed`` с исходной причиной), её задача workflow раз за
        разом падает (``SessionStuck``) или — для ещё не обработанного
        ``pending_message`` — завершилась иначе (закрыта, прервана)."""
        from temporalio.client import (
            WorkflowExecutionStatus,
            WorkflowFailureError,
        )

        try:
            description = await self.handle.describe()
        except Exception:
            return None
        if description.status in (
            WorkflowExecutionStatus.RUNNING,
            WorkflowExecutionStatus.CONTINUED_AS_NEW,
        ):
            task = description.raw_description.pending_workflow_task
            attempt = task.attempt if task is not None else 0
            if stuck_attempts and attempt >= stuck_attempts:
                return SessionStuck(
                    f"Задача workflow {self.handle.id} падает (попытка "
                    f"{attempt}): причина — в логе воркера и в истории "
                    "(WorkflowTaskFailed). Сессия продолжится, когда сбой "
                    "устранят (например, исправленный воркер)."
                )
            return None
        if description.status != WorkflowExecutionStatus.FAILED:
            if pending_message is None:
                return None
            return SessionFailed(
                f"Сессия завершилась ({description.status.name}), сообщение "
                f"{pending_message} не обработано"
            )
        try:
            await self.handle.result(follow_runs=False)
        except WorkflowFailureError as failure:
            # Суть — в глубине цепочки причин (ApplicationError отказа хода).
            cause, message = failure, None
            while cause is not None:
                message = getattr(cause, "message", None) or message
                cause = getattr(cause, "cause", None)
            error = SessionFailed(message or str(failure))
            error.__cause__ = failure
            return error
        return None

    async def ask(self, text, *, message_id=None, timeout=None):
        """Отправить и дождаться: ответ хода (как ``AsyncAgent.run``).

        Сообщение, отброшенное остановкой хода, — ``MessageNotProcessed``:
        ответа на него нет, а ответ прошлого хода выдавать за него нельзя.
        ``timeout`` (секунды) — как у ``wait``: истёк — ``TimeoutError``,
        сообщение остаётся в сессии (дождаться его можно ``wait``).
        """
        message_id = await self.send(text, message_id=message_id)
        reply = await self.wait(message_id, timeout=timeout)
        if not reply["delivered"]:
            raise MessageNotProcessed(reply["message_id"])
        _raise_turn_error(reply)
        return reply["answer"]

    async def answer_human(self, request_id, answer, *, reply_id=None):
        return await self.handle.execute_update(
            UniversalAgentWorkflow.answer_human,
            args=[request_id, answer, reply_id],
        )

    async def stop(self):
        """Остановить идущий ход (кооперативно, как ``AsyncAgent.stop``)."""
        await self.handle.signal(UniversalAgentWorkflow.stop)

    async def status(self):
        return await self.handle.query(UniversalAgentWorkflow.status)

    async def close(self):
        """Закрыть сессию после принятой работы и снять владение тредом."""
        await self.handle.signal(UniversalAgentWorkflow.close_session)


class TemporalDurability:
    """Исполнение ``AsyncAgent`` через Temporal (``durable=``).

    ``AsyncAgent.create(settings, durable=TemporalDurability(client,
    task_queue="shop", agent="shop"))`` — тот же API агента, а ходы исполняет
    воркер с ``AgentPlugin``. Настройки агента читает воркер (его профиль
    ``agent``); ``settings`` приложения нужны только локальному режиму.
    """

    def __init__(
        self,
        client,
        *,
        task_queue,
        agent,
        store_id="default",
        resource_queue=None,
        model_queue=None,
    ):
        self.client = client
        self.options = {
            "task_queue": task_queue,
            "agent": agent,
            "store_id": store_id,
            "resource_queue": resource_queue,
            "model_queue": model_queue,
        }

    async def open(self, settings):
        return _DurableAgentRuntime(
            self.client, {**self.options, "settings": settings}
        )


def _with_ids(messages, message_ids):
    """Пары ``(текст, ID | None)``: ID — по порядку, сколько задано."""
    ids = list(message_ids or [])
    return [
        (text, ids[index] if index < len(ids) else None)
        for index, text in enumerate(messages)
    ]


class _DurableAgentRuntime:
    """Сторона ``AsyncAgent`` в durable-режиме — с той же семантикой, что
    локальный ``AsyncAgent``:

    - ``send`` вне хода откладывает сообщение до следующего ``run`` (локально
      оно ждёт в очереди исполнителя) — новый ход само по себе не запускает;
    - ``send`` во время хода отдаёт сообщение в идущий ход, и ``run`` ждёт
      ход, который его обработает («хвост», как локально), и возвращает
      ответ последнего хода;
    - ``stop`` останавливает идущий ход; вне хода ничего не делает (локально
      ``run`` тоже начинается со сброса остановки);
    - сбой фоновой отправки не теряется: он поднимается следующим ``run``.
    """

    def __init__(self, client, options):
        self.client, self.options = client, options
        self._active = None
        self._running = False
        #: Отложенные вне хода сообщения: (текст, ID) — уйдут со следующим run.
        self._deferred = []
        #: ID сообщений, отданных идущему ходу через send (хвост run).
        self._tail = []
        self._background = set()
        self._errors = []

    def _session(self, thread_id, system):
        return AgentSession(
            self.client,
            thread_id=thread_id,
            system_id=system,
            **self.options,
        )

    def _spawn(self, coroutine):
        task = asyncio.ensure_future(coroutine)
        self._background.add(task)
        task.add_done_callback(self._settled)
        return task

    def _settled(self, task):
        self._background.discard(task)
        if not task.cancelled() and task.exception() is not None:
            logger.warning("durable send/stop failed: %r", task.exception())
            self._errors.append(task.exception())

    async def run(
        self, messages, *, thread_id=None, system="sdk", message_ids=None
    ):
        if self._running:
            # Состояние хода (сессия, хвост) — одно на агента, как у
            # локального исполнителя: второй run разошёлся бы с первым.
            raise RuntimeError(
                "run уже идёт: durable-агент ведёт один ход за раз"
            )
        if self._errors:
            raise self._errors.pop(0)
        # Без thread_id у локального агента нет истории: здесь — новый тред
        # на каждый вызов.
        ephemeral = thread_id is None
        session = self._session(thread_id or uuid.uuid4().hex, system)
        batch = _with_ids(messages, message_ids)
        deferred = list(self._deferred)
        self._active, self._running, self._tail = session, True, []
        try:
            # Все сообщения — одним update, в один ход (как локальный run).
            last = (await session.send_batch([*batch, *deferred]))[-1]
            # Отложенные send ушли с этим ходом; сбой отправки их не теряет.
            del self._deferred[: len(deferred)]
            reply = await self._wait_tail(session, await session.wait(last))
            if not reply["delivered"] or self._tail:
                # Ход остановили: хвост не доигрывается (как локально), но и
                # не теряется молча.
                logger.warning(
                    "run_tail_dropped thread_id=%s count=%d",
                    session.request.thread_id,
                    len(self._tail) + (0 if reply["delivered"] else 1),
                )
            _raise_turn_error(reply)
            return reply["answer"]
        finally:
            self._running, self._tail = False, []
            if ephemeral:
                # Без thread_id тред одноразовый (локально — без истории):
                # закрыть сессию, а не оставить её вечно держать тред. Сбой
                # закрытия (сессия уже упала) не подменяет исход хода.
                with contextlib.suppress(Exception):
                    await session.close()

    async def _wait_tail(self, session, reply):
        """Хвост: присланное send() во время хода — его ходы тоже часть
        этого run, ответ — последнего из них (как локальный run)."""
        while reply["delivered"] and (self._tail or self._background):
            if self._background:
                await asyncio.gather(*self._background, return_exceptions=True)
            if self._errors:
                raise self._errors.pop(0)
            if not self._tail:
                break
            reply = await session.wait(self._tail.pop(0))
        return reply

    def send(self, message):
        text = str(message.get("content", ""))
        message_id = (
            message.get("message_id") or message.get("id") or uuid.uuid4().hex
        )
        if not self._running:
            self._deferred.append((text, message_id))
            return
        self._tail.append(message_id)
        self._spawn(self._active.send(text, message_id=message_id))

    def stop(self):
        if self._running and self._active is not None:
            self._spawn(self._active.stop())

    async def aclose(self):
        if self._background:
            await asyncio.gather(*self._background, return_exceptions=True)


async def _unlock(args, temporal):
    """``unlock``: снять владение тредом у сессии, которой больше нет."""
    from gc_core_temporalio.provider import WorkerConfig
    from gc_core_temporalio.thread_store_activities import release_stale_owner
    from gc_core_temporalio.worker import build_stores

    if not args.config:
        raise SystemExit(
            "unlock: нужен --config (JSON воркера с thread_stores)"
        )
    stores = build_stores(WorkerConfig.load(args.config))
    if args.store not in stores:
        raise SystemExit(
            f"unlock: хранилища {args.store!r} нет в конфиге воркера; "
            f"есть: {', '.join(sorted(stores)) or 'никаких'}"
        )
    released = await release_stale_owner(
        stores[args.store], args.system, args.thread, temporal
    )
    if released is None:
        return "Тред свободен или его сессия ещё исполняется: ничего не снято"
    return f"Снято владение сессии {released}"


async def _run(args):
    client = AgentClient(
        await Client.connect(args.address, namespace=args.namespace),
        workflow_queue=args.workflow_queue,
    )
    if args.command == "unlock":
        print(await _unlock(args, client.client))
        return
    if args.command == "start":
        handle = await client.start(
            RunRequest(
                format_text=read_format_description(
                    args.structured_output_format
                )
                if args.structured_output_format
                else None,
                profile=args.profile,
                store_id=args.store,
                system_id=args.system,
                thread_id=args.thread,
                messages=[args.prompt],
                resource_queue=args.resource_queue,
                model_queue=args.model_queue,
                keep_alive=args.keep_alive,
            ),
            workflow_id=args.workflow_id,
        )
        print(handle.id)
    elif args.command == "send":
        print(
            await client.submit_message(
                args.workflow_id, UserMessage(args.message_id, args.text)
            )
        )
    elif args.command == "close":
        await client.close_session(args.workflow_id)
    elif args.command == "answer":
        print(
            await client.answer_human(
                args.workflow_id, args.request_id, args.answer
            )
        )
    elif args.command == "stop":
        await client.stop(args.workflow_id)
    else:
        value = await getattr(client, args.command)(args.workflow_id)
        print(json.dumps(value, ensure_ascii=False, indent=2))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--address", default="localhost:7233")
    parser.add_argument("--namespace", default="default")
    parser.add_argument(
        "--config",
        help="worker.json воркера (для unlock: хранилища тредов)",
    )
    parser.add_argument("--workflow-queue", default="ua-workflow")
    commands = parser.add_subparsers(dest="command", required=True)
    start = commands.add_parser("start")
    for name in ("profile", "thread", "prompt"):
        start.add_argument(f"--{name}", required=True)
    start.add_argument("--system", default="default")
    start.add_argument("--store", default="files")
    start.add_argument("--resource-queue", default="ua-resource-local")
    start.add_argument("--model-queue", default="ua-model")
    start.add_argument("--workflow-id")
    start.add_argument("--structured-output-format")
    start.add_argument("--keep-alive", action="store_true")
    unlock = commands.add_parser(
        "unlock",
        help="снять владение тредом у завершённой или снятой сессии",
    )
    unlock.add_argument("--thread", required=True)
    unlock.add_argument("--system", default="default")
    unlock.add_argument("--store", default="files")
    for name in (
        "status",
        "result",
        "stop",
        "answer",
        "close",
        "send",
    ):
        sub = commands.add_parser(name)
        sub.add_argument("workflow_id")
        if name == "send":
            sub.add_argument("--message-id", required=True)
            sub.add_argument("--text", required=True)
        if name == "answer":
            sub.add_argument("--request-id", required=True)
            sub.add_argument("--answer", required=True)
    asyncio.run(_run(parser.parse_args()))


if __name__ == "__main__":
    main()
