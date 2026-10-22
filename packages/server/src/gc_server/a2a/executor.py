"""Исполнитель задач A2A для SDK: диалог — тред ``system="a2a"``
(``contextId`` — id треда), ход агента — в фоне.

Вопрос агента человеку (``turn_ask_human``) завершает ход — приостановка
ядра (``ask_human_suspends``): задача ``INPUT_REQUIRED``, тред свободен,
агент ничего не ждёт. Ответ — следующее сообщение с тем же ``taskId`` на
любом инстансе: ход продолжается с ответом.

Диалог ведёт задачи по очереди. Новая задача в занятом диалоге (идёт ход,
или задача ждёт ответа на вопрос) — ``SUBMITTED``: она в очереди диалога
(``frontend.a2a``, записи ``q:<taskId>``, порядок — по времени приёма) и
ждёт в том же запросе (``execute``): события задачи идут в его очередь
событий SDK — статусы и push как у обычной задачи. Отмена из очереди —
``CANCELED``. Восстановление (``arecover_once``) переводит в ``FAILED``
задачи, чей ход или ожидание очереди умерли вместе с инстансом, — очередь
диалога идёт дальше.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from datetime import timedelta
from typing import Any

from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2 as pb
from gc_core.execution.environment import utc_now
from gc_core.threads.facade import Threads
from gc_core.threads.instance import instance_id
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.threads.rules import lease_expired
from gc_core.tools.turn.propose_plan import ProposePlan
from gc_core.tools.turn.submit_solution import SubmitSolution
from google.protobuf.json_format import MessageToDict

from gc_server.a2a.agent_config import prepare_agent_settings
from gc_server.a2a.task_store import (
    FRONTEND,
    SYSTEM,
    ThreadTaskStore,
    dialog_key,
    task_index,
)

logger = logging.getLogger(__name__)

# Живых агентов на процесс, как у Responses. Вытесняется самый давний
# простаивающий; ход в работе не закрывается.
MAX_LIVE_AGENTS = 32
# Пиру в status.message — без путей и текста исключения ядра.
PEER_FAILURE_TEXT = "Ход не выполнен."
NOT_WAITING_TEXT = "Задача не ждёт ответа: вопроса, на который отвечать, нет."
INTERRUPTED_TEXT = "Ход прерван: инстанс, который вёл задачу, остановился."
CWD_TEXT = (
    "metadata.cwd должен быть абсолютным путём внутри корня агента и "
    "совпадать с диалогом."
)
# Досрочная остановка — не ответ: бюджет (шаги, токены, тулы) и недоступная
# проверка действий судьёй. JSON ``ok: false`` в артефакт не кладём.
_STOPS = frozenset(
    {"step_limit", "token_limit", "tool_call_limit", "judge_unavailable"}
)
#: Ключи состояния фронтенда ``a2a`` треда диалога.
_CWD_KEY = "cwd"
#: Задача, чей ход идёт: ``{task, instance, at}``.
_RUNNING = "running"
#: Задача, ждущая ответа на вопрос (держит очередь диалога).
_WAITING = "waiting"
#: Очередь диалога: ``q:<taskId>`` → ``{at, seen, instance}``.
_QUEUE = "q:"
#: Отменённая в очереди задача: ``x:<taskId>`` → ``true``.
_CANCELED = "x:"
#: Как часто ждущая в очереди задача проверяет диалог (и отмечается живой).
QUEUE_POLL_SECONDS = 2.0
#: Сколько молчит ожидание или ход умершего инстанса, прежде чем
#: восстановление сочтёт его мёртвым.
STALE_AFTER = timedelta(seconds=60)


def answer_text(agent: Any) -> str:
    last = getattr(agent, "last_answer", None)
    # Ход, закрытый планом: пиру — текст плана. Одобрения в A2A нет:
    # согласие пира — не согласие пользователя.
    plan = ProposePlan.present_result(last)
    if plan is not None:
        return plan
    raw = SubmitSolution.present_result(last)
    if raw is None:
        return ""
    return raw if isinstance(raw, str) else str(raw)


def stop_outcome(agent: Any) -> str | None:
    executor = getattr(agent, "executor", None)
    outcome = getattr(executor, "last_terminal_outcome", None)
    if isinstance(outcome, str) and outcome in _STOPS:
        return outcome
    return None


class _CwdError(ValueError):
    pass


def _now() -> str:
    return utc_now().isoformat()


def _stale(stamp: Any) -> bool:
    from datetime import datetime

    try:
        moment = datetime.fromisoformat(str(stamp))
    except ValueError:
        return True
    return utc_now() - moment > STALE_AFTER


def _dialog_state(record: ThreadRecord | None) -> dict[str, Any]:
    return dict((record.frontend.get(FRONTEND) or {}) if record else {})


def _queue(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Очередь диалога по времени приёма: ``(taskId, запись)``."""
    entries = [
        (name[len(_QUEUE) :], value)
        for name, value in state.items()
        if name.startswith(_QUEUE) and isinstance(value, dict)
    ]
    return sorted(entries, key=lambda item: (str(item[1].get("at")), item[0]))


def _lease_live(record: ThreadRecord | None) -> bool:
    lease = record.lease if record is not None else None
    return lease is not None and not lease_expired(lease, utc_now())


class Turn:
    """Ход задачи в фоне: новый запрос или продолжение с ответом."""

    def __init__(
        self,
        agent: Any,
        task_id: str,
        context_id: str,
        prompt: Any,
        *,
        answer: str | None = None,
    ):
        self.agent = agent
        self.task_id = task_id
        self.context_id = context_id
        self._prompt = prompt
        self._answer = answer
        self.cancel_requested = False
        self.failed: BaseException | None = None
        self.done = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def cancel(self) -> None:
        self.cancel_requested = True
        stop = getattr(self.agent, "stop", None)
        if stop is not None:
            stop()
        await self.done.wait()

    async def _run(self) -> None:
        try:
            await self._forward()
        except asyncio.CancelledError:
            self.cancel_requested = True
        except Exception as exc:
            if not self.cancel_requested:
                self.failed = exc
                logger.exception("a2a turn")
        self.done.set()

    async def _forward(self) -> None:
        extra = {} if self._answer is None else {"answer": self._answer}
        stream = self.agent.stream(
            self._prompt, thread_id=self.context_id, system=SYSTEM, **extra
        )
        try:
            async for _kind, _text in stream:
                if self.cancel_requested:
                    break
        finally:
            # ``return``/``break`` из ``async for`` не ждут ``finally``
            # генератора: прогон ``AsyncAgent.stream`` остался бы жить.
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()


class GcAgentExecutor(AgentExecutor):
    """``AgentExecutor`` SDK: ход агента на задачу A2A."""

    def __init__(
        self,
        *,
        settings: Any,
        agent_factory: Any,
        threads: Threads,
        launch_root: str | None = None,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
    ) -> None:
        self._settings = settings
        self._agent_factory = agent_factory
        self._threads = threads
        self._tasks = ThreadTaskStore(threads)
        self._launch_root = launch_root
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._turns: dict[str, Turn] = {}
        #: Задачи этого инстанса, ждущие очереди диалога.
        self._waiting: set[str] = set()
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._create_lock = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        if (
            task is not None
            and task.status.state == pb.TASK_STATE_INPUT_REQUIRED
        ):
            # Ответ может прийти без contextId: диалог — у самой задачи.
            updater = TaskUpdater(event_queue, task.id, task.context_id)
            await self._aanswer(task, context.get_user_input(), updater)
            return
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        if task is None:
            await event_queue.enqueue_event(
                new_task_from_user_message(context.message)
            )
        await self._abegin(context, updater)

    async def cancel(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        context_id = (
            task.context_id if task is not None else context.context_id
        )
        updater = TaskUpdater(event_queue, context.task_id, context_id)
        turn = self._turns.get(context.task_id)
        if turn is not None:
            await turn.cancel()
        else:
            await self._acancel_elsewhere(context_id, context.task_id)
        await updater.cancel()

    async def _acancel_elsewhere(self, context_id: str, task_id: str) -> None:
        """Задача ждёт ответа — диалог свободен (вопрос закроет следующий
        ход); в очереди — снимается с неё; ход на другом инстансе — отмена
        через поток control треда."""
        key = dialog_key(context_id)
        state = _dialog_state(await self._threads.get(key))
        if state.get(_WAITING) == task_id:
            await self._threads.update_frontend(
                key, FRONTEND, {_WAITING: None}
            )
        elif _QUEUE + task_id in state:
            await self._threads.update_frontend(
                key,
                FRONTEND,
                {_QUEUE + task_id: None, _CANCELED + task_id: True},
            )
        else:
            await self._threads.request_cancel(key)

    async def shutdown(self) -> None:
        # Остановка сервера — прерывание: задачи остаются как есть, их
        # подберёт восстановление другого инстанса.
        for turn in list(self._turns.values()):
            await turn.cancel()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()

    async def _abegin(self, context: RequestContext, updater: TaskUpdater):
        try:
            cwd = await self._cwd_for(context)
        except _CwdError:
            await updater.failed(_agent_message(updater, CWD_TEXT))
            return
        task_id, context_id = context.task_id, context.context_id
        if not await self._aawait_turn(context_id, task_id, cwd):
            return  # отменена в очереди: итог — cancel()
        try:
            agent = await self._agent_for(context_id, cwd)
        except Exception:
            logger.exception("a2a agent")
            await self._afree(context_id, task_id)
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            return
        await updater.start_work()
        turn = Turn(agent, task_id, context_id, context.get_user_input())
        self._turns[task_id] = turn
        turn.start()
        await self._report(turn, updater)

    async def _aanswer(self, task: pb.Task, text: str, updater: TaskUpdater):
        """Ответ на вопрос задачи: ход продолжается с ответом — на любом
        инстансе (тред и вопрос — в хранилище)."""
        task_id, context_id = task.id, task.context_id
        key = dialog_key(context_id)
        state = _dialog_state(await self._threads.get(key))
        if state.get(_WAITING) != task_id:
            await updater.failed(_agent_message(updater, NOT_WAITING_TEXT))
            return
        await self._threads.update_frontend(
            key, FRONTEND, {_WAITING: None, _RUNNING: self._claim(task_id)}
        )
        try:
            agent = await self._agent_for(context_id, state.get(_CWD_KEY))
        except Exception:
            logger.exception("a2a agent")
            await self._afree(context_id, task_id)
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            return
        await updater.start_work()
        turn = Turn(agent, task_id, context_id, [], answer=text)
        self._turns[task_id] = turn
        turn.start()
        await self._report(turn, updater)

    async def _aawait_turn(self, context_id: str, task_id: str, cwd: str):
        """Встать в очередь диалога и дождаться своей очереди: диалог
        свободен (нет хода, никто не ждёт ответа) и задача первая. ``False``
        — задачу отменили в очереди."""
        key = dialog_key(context_id)
        entry = {"at": _now(), "instance": instance_id()}
        await self._threads.update_frontend(
            key,
            FRONTEND,
            {_CWD_KEY: cwd, _QUEUE + task_id: {**entry, "seen": _now()}},
        )
        self._waiting.add(task_id)
        try:
            while True:
                record = await self._threads.get(key)
                state = _dialog_state(record)
                if state.get(_CANCELED + task_id):
                    return False
                if self._dialog_free(record, state) and _queue(state)[:1] == [
                    (task_id, state[_QUEUE + task_id])
                ]:
                    await self._threads.update_frontend(
                        key,
                        FRONTEND,
                        {
                            _QUEUE + task_id: None,
                            _RUNNING: self._claim(task_id),
                        },
                    )
                    return True
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                await self._threads.update_frontend(
                    key,
                    FRONTEND,
                    {_QUEUE + task_id: {**entry, "seen": _now()}},
                )
        finally:
            self._waiting.discard(task_id)

    @staticmethod
    def _claim(task_id: str) -> dict[str, Any]:
        return {"task": task_id, "instance": instance_id(), "at": _now()}

    @staticmethod
    def _dialog_free(
        record: ThreadRecord | None, state: dict[str, Any]
    ) -> bool:
        """Диалог свободен: хода нет (ни отметки, ни живой аренды) и никто
        не ждёт ответа на вопрос."""
        return (
            not state.get(_RUNNING)
            and not state.get(_WAITING)
            and not _lease_live(record)
        )

    async def _afree(self, context_id: str, task_id: str) -> None:
        """Снять отметку хода задачи (своей) — диалог свободен."""
        key = dialog_key(context_id)
        running = _dialog_state(await self._threads.get(key)).get(_RUNNING)
        if isinstance(running, dict) and running.get("task") == task_id:
            await self._threads.update_frontend(
                key, FRONTEND, {_RUNNING: None}
            )

    async def _report(self, turn: Turn, updater: TaskUpdater) -> None:
        """Исход хода: вопрос — ``INPUT_REQUIRED`` (тред свободен, задача
        держит очередь диалога), иначе итог задачи."""
        await turn.done.wait()
        self._turns.pop(turn.task_id, None)
        key = dialog_key(turn.context_id)
        if turn.cancel_requested:
            await self._afree(turn.context_id, turn.task_id)
            return  # итог — cancel() (или остановка сервера)
        question = getattr(turn.agent, "pending_question", None)
        if turn.failed is None and question:
            await self._threads.update_frontend(
                key, FRONTEND, {_RUNNING: None, _WAITING: turn.task_id}
            )
            await updater.requires_input(
                _agent_message(updater, str(question.get("question") or ""))
            )
            return
        await self._afree(turn.context_id, turn.task_id)
        if turn.failed is not None or stop_outcome(turn.agent) is not None:
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            return
        await updater.add_artifact(
            [pb.Part(text=answer_text(turn.agent))], name="answer"
        )
        await updater.complete()

    # --- восстановление ------------------------------------------------

    async def arecover_once(self) -> int:
        """Задачи, чей ход или ожидание очереди умерли вместе с инстансом, —
        ``FAILED`` (прерваны); их место в диалоге освобождается. Возвращает,
        сколько задач прервано."""
        failed = 0
        async for record in self._threads.iter_threads(system=SYSTEM):
            failed += await self._arecover_dialog(record)
        return failed

    async def _arecover_dialog(self, record: ThreadRecord) -> int:
        state = _dialog_state(record)
        dead: list[str] = []
        patch: dict[str, Any] = {}
        running = state.get(_RUNNING)
        if (
            isinstance(running, dict)
            and running.get("task") not in self._turns
            and not _lease_live(record)
            and _stale(running.get("at"))
        ):
            dead.append(str(running.get("task")))
            patch[_RUNNING] = None
        for task_id, entry in _queue(state):
            if task_id not in self._waiting and _stale(entry.get("seen")):
                dead.append(task_id)
                patch[_QUEUE + task_id] = None
        if not dead:
            return 0
        await self._threads.update_frontend(record.key, FRONTEND, patch)
        index = task_index(record)
        for task_id in dead:
            await self._ainterrupt(record.key, index.get(task_id))
        return len(dead)

    async def _ainterrupt(self, key: ThreadKey, position: Any) -> None:
        task = await self._tasks.at(key, position)
        if task is None or task.status.state in _FINAL_STATES:
            return
        task.status.state = pb.TASK_STATE_FAILED
        task.status.message.CopyFrom(
            pb.Message(
                role=pb.ROLE_AGENT,
                message_id=f"interrupted-{task.id}",
                task_id=task.id,
                context_id=task.context_id,
                parts=[pb.Part(text=INTERRUPTED_TEXT)],
            )
        )
        task.status.timestamp.GetCurrentTime()
        await self._tasks.save(task, None)

    # --- корень хода и агенты -------------------------------------------

    async def _cwd_for(self, context: RequestContext) -> str:
        sent = _sent_cwd(context)
        known = await self._dialog_cwd(context.context_id)
        if known is not None:
            if sent is not None and os.path.realpath(sent) != known:
                raise _CwdError(sent)
            return known
        root = self._root()
        if sent is None:
            return root
        if not os.path.isabs(sent):
            raise _CwdError(sent)
        # Корень хода — периметр записи агента: пир выбирает его только
        # внутри корня поверхности. Проверяется и отдаётся ходу раскрытый
        # путь (симлинки и «..»): подмена компонента после проверки не
        # уведёт ход за периметр.
        cwd = os.path.realpath(sent)
        boundary = os.path.realpath(root)
        if os.path.commonpath([boundary, cwd]) != boundary:
            raise _CwdError(sent)
        return cwd

    def _root(self) -> str:
        """Корень поверхности: a2a.launch_root или каталог процесса."""
        root = self._launch_root
        if isinstance(root, str) and os.path.isabs(root):
            return os.path.normpath(root)
        return os.path.normpath(os.getcwd())

    async def _dialog_cwd(self, context_id: str) -> str | None:
        """Корень диалога — из прошлых задач контекста (на любом инстансе)."""
        cwd = _dialog_state(
            await self._threads.get(dialog_key(context_id))
        ).get(_CWD_KEY)
        return cwd if isinstance(cwd, str) and cwd else None

    async def _agent_for(self, context_id: str, cwd: Any) -> Any:
        cached = self._agents.get(context_id)
        if cached is not None:
            self._agents.move_to_end(context_id)
            return cached
        async with self._create_lock:
            cached = self._agents.get(context_id)
            if cached is not None:
                return cached
            agent = await self._agent_factory(
                prepare_agent_settings(
                    self._settings, cwd=str(cwd or self._root())
                ),
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )
            self._agents[context_id] = agent
            await self._evict_idle()
            return agent

    async def _evict_idle(self) -> None:
        busy = {t.context_id for t in self._turns.values()}
        idle = [key for key in self._agents if key not in busy]
        for key in idle[: max(0, len(self._agents) - MAX_LIVE_AGENTS)]:
            await _safe_aclose(self._agents.pop(key))


_FINAL_STATES = frozenset(
    {
        pb.TASK_STATE_COMPLETED,
        pb.TASK_STATE_FAILED,
        pb.TASK_STATE_CANCELED,
        pb.TASK_STATE_REJECTED,
    }
)


def _sent_cwd(context: RequestContext) -> str | None:
    """``metadata.cwd`` — у сообщения или у запроса."""
    message = context.message
    if message is not None and message.HasField("metadata"):
        cwd = MessageToDict(message.metadata).get("cwd")
        if isinstance(cwd, str):
            return cwd
    cwd = (context.metadata or {}).get("cwd")
    return cwd if isinstance(cwd, str) else None


def _agent_message(updater: TaskUpdater, text: str) -> pb.Message:
    return updater.new_agent_message([pb.Part(text=text)])


async def _safe_aclose(agent: Any) -> None:
    close = getattr(agent, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("a2a aclose")
