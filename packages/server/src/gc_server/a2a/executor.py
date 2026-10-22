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
import contextlib
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
    CANCELED,
    FINAL_STATES,
    FRONTEND,
    QUEUE,
    RUNNING,
    SYSTEM,
    WAITING,
    ThreadTaskStore,
    dialog_key,
    dialog_state,
    task_index,
)

logger = logging.getLogger(__name__)

# Живых агентов на процесс, как у Responses. Вытесняется самый давний
# простаивающий; ход в работе не закрывается.
MAX_LIVE_AGENTS = 32
# Пиру в status.message — без путей и текста исключения ядра.
PEER_FAILURE_TEXT = "Ход не выполнен."
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


def _queue(state: dict[str, Any]) -> list[tuple[str, dict[str, Any]]]:
    """Очередь диалога по времени приёма: ``(taskId, запись)``; отменённые
    задачи очередь не держат."""
    entries = [
        (name[len(QUEUE) :], value)
        for name, value in state.items()
        if name.startswith(QUEUE)
        and isinstance(value, dict)
        and not state.get(CANCELED + name[len(QUEUE) :])
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
        #: Ход остановлен вместе с сервером: задача не отменена — её
        #: прервёт восстановление другого инстанса.
        self.interrupted = False
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
            self._prompt,
            thread_id=self.context_id,
            system=SYSTEM,
            # Отмена этой задачи с другого инстанса называет её.
            cancel_target=self.task_id,
            **extra,
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
        #: Пульс отметки хода задач этого инстанса.
        self._pulses: dict[str, asyncio.Task] = {}
        #: Задачи этого инстанса, ждущие очереди диалога: запись очереди.
        self._waiting: dict[str, dict[str, Any]] = {}
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
        if task is not None:
            # Задача уже не ждёт сообщения (повтор, проскочивший проверку
            # запроса): второго хода нет, статус не трогаем.
            return
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        try:
            cwd = await self._cwd_for(context)
        except _CwdError:
            cwd = None
        else:
            # Место в очереди — до ответа пиру: задача, о приёме которой
            # он узнал, в очереди диалога раньше следующей.
            await self._aenqueue(context.context_id, context.task_id, cwd)
        try:
            if task is None:
                await event_queue.enqueue_event(
                    new_task_from_user_message(context.message)
                )
            if cwd is None:
                await updater.failed(_agent_message(updater, CWD_TEXT))
            else:
                await self._abegin(context, updater, cwd)
        finally:
            self._waiting.pop(context.task_id, None)

    async def cancel(self, context: RequestContext, event_queue: Any) -> None:
        task = context.current_task
        context_id = (
            task.context_id if task is not None else context.context_id
        )
        updater = TaskUpdater(event_queue, context.task_id, context_id)
        turn = self._turns.get(context.task_id)
        if turn is not None:
            await turn.cancel()
            # Запрос задачи (с ``_report``) SDK при отмене снимает: место в
            # диалоге освобождаем сами — очередь идёт сразу.
            await self._astop_pulse(context.task_id)
            await self._afree(context_id, context.task_id)
        # Ожидание ответа, место в очереди и ход на другом инстансе снимает
        # снимок ``CANCELED`` (``ThreadTaskStore.save``) — так же, как
        # отмену, которую SDK пишет мимо исполнителя.
        await updater.cancel()

    async def shutdown(self) -> None:
        # Остановка сервера — прерывание: задачи остаются как есть, их
        # подберёт восстановление другого инстанса.
        for turn in list(self._turns.values()):
            turn.interrupted = True
            await turn.cancel()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()

    async def _abegin(
        self, context: RequestContext, updater: TaskUpdater, cwd: str
    ):
        task_id, context_id = context.task_id, context.context_id
        if not await self._aawait_turn(context_id, task_id):
            return  # отменена в очереди: итог записан отменой
        await self._arun_claimed(
            updater, task_id, context_id, cwd, context.get_user_input()
        )

    async def _aanswer(self, task: pb.Task, text: str, updater: TaskUpdater):
        """Ответ на вопрос задачи: ход продолжается с ответом — на любом
        инстансе (тред и вопрос — в хранилище)."""
        task_id, context_id = task.id, task.context_id
        key = dialog_key(context_id)
        state = dialog_state(await self._threads.get(key))
        if state.get(WAITING) != task_id:
            # Ответ уже принят (повтор с другого инстанса) или задачу
            # отменили: этот запрос задачу не меняет.
            return
        await self._threads.update_frontend(
            key, FRONTEND, {WAITING: None, RUNNING: self._claim(task_id)}
        )
        await self._arun_claimed(
            updater, task_id, context_id, state.get(_CWD_KEY), [], answer=text
        )

    async def _arun_claimed(
        self,
        updater: TaskUpdater,
        task_id: str,
        context_id: str,
        cwd: Any,
        prompt: Any,
        *,
        answer: str | None = None,
    ) -> None:
        """Ход задачи, занявшей диалог: агент, ход, итог. Пока агент
        создаётся и ход идёт, отметка хода живая (пульс) — восстановление
        другого инстанса прерывает только умершую вместе с инстансом."""
        self._pulses[task_id] = asyncio.create_task(
            self._apulse(context_id, task_id)
        )
        try:
            try:
                agent = await self._agent_for(context_id, cwd)
            except Exception:
                logger.exception("a2a agent")
                await self._astop_pulse(task_id)
                await self._afree(context_id, task_id)
                await updater.failed(
                    _agent_message(updater, PEER_FAILURE_TEXT)
                )
                return
            await updater.start_work()
            turn = Turn(agent, task_id, context_id, prompt, answer=answer)
            self._turns[task_id] = turn
            turn.start()
            await turn.done.wait()
            await self._astop_pulse(task_id)
            await self._report(turn, updater)
        finally:
            await self._astop_pulse(task_id)

    async def _apulse(self, context_id: str, task_id: str) -> None:
        """Обновлять ``running.at`` своей отметки, пока она своя."""
        key = dialog_key(context_id)
        while True:
            await asyncio.sleep(STALE_AFTER.total_seconds() / 3)
            running = dialog_state(await self._threads.get(key)).get(RUNNING)
            if not (
                isinstance(running, dict) and running.get("task") == task_id
            ):
                return
            await self._threads.update_frontend(
                key, FRONTEND, {RUNNING: {**running, "at": _now()}}
            )

    async def _astop_pulse(self, task_id: str) -> None:
        """Пульс — до того, как отметку снимут: иначе он вернул бы её."""
        pulse = self._pulses.pop(task_id, None)
        if pulse is None:
            return
        pulse.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await pulse

    async def _aenqueue(self, context_id: str, task_id: str, cwd: str):
        """Встать в очередь диалога (порядок — по времени приёма)."""
        entry = {"at": _now(), "instance": instance_id()}
        self._waiting[task_id] = entry
        await self._threads.update_frontend(
            dialog_key(context_id),
            FRONTEND,
            {_CWD_KEY: cwd, QUEUE + task_id: {**entry, "seen": _now()}},
        )

    async def _aawait_turn(self, context_id: str, task_id: str) -> bool:
        """Дождаться своей очереди: диалог свободен (нет хода, никто не
        ждёт ответа) и задача первая. ``False`` — задачу отменили в
        очереди."""
        key = dialog_key(context_id)
        entry = self._waiting[task_id]
        try:
            while True:
                record = await self._threads.get(key)
                state = dialog_state(record)
                if state.get(CANCELED + task_id):
                    if QUEUE + task_id in state:
                        # Отметка «жив» могла вернуть снятую запись.
                        await self._threads.update_frontend(
                            key, FRONTEND, {QUEUE + task_id: None}
                        )
                    return False
                if self._dialog_free(record, state) and _queue(state)[:1] == [
                    (task_id, state[QUEUE + task_id])
                ]:
                    await self._threads.update_frontend(
                        key,
                        FRONTEND,
                        {
                            QUEUE + task_id: None,
                            RUNNING: self._claim(task_id),
                        },
                    )
                    return True
                await asyncio.sleep(QUEUE_POLL_SECONDS)
                await self._threads.update_frontend(
                    key,
                    FRONTEND,
                    {QUEUE + task_id: {**entry, "seen": _now()}},
                )
        finally:
            self._waiting.pop(task_id, None)

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
            not state.get(RUNNING)
            and not state.get(WAITING)
            and not _lease_live(record)
        )

    async def _afinished(self, task_id: str) -> bool:
        """Задачу закрыли мимо хода (отмена с другого инстанса)."""
        task = await self._tasks.latest(task_id)
        return task is not None and task.status.state in FINAL_STATES

    async def _afree(self, context_id: str, task_id: str) -> None:
        """Снять отметку хода задачи (своей) — диалог свободен."""
        key = dialog_key(context_id)
        running = dialog_state(await self._threads.get(key)).get(RUNNING)
        if isinstance(running, dict) and running.get("task") == task_id:
            await self._threads.update_frontend(key, FRONTEND, {RUNNING: None})

    async def _report(self, turn: Turn, updater: TaskUpdater) -> None:
        """Исход хода: вопрос — ``INPUT_REQUIRED`` (тред свободен, задача
        держит очередь диалога), иначе итог задачи."""
        await turn.done.wait()
        self._turns.pop(turn.task_id, None)
        key = dialog_key(turn.context_id)
        if turn.interrupted:
            return  # отметка хода остаётся: задачу прервёт восстановление
        if turn.cancel_requested or await self._afinished(turn.task_id):
            # Итог уже записан: cancel() или отмена мимо исполнителя.
            await self._afree(turn.context_id, turn.task_id)
            return
        question = getattr(turn.agent, "pending_question", None)
        if turn.failed is None and question:
            await self._threads.update_frontend(
                key, FRONTEND, {RUNNING: None, WAITING: turn.task_id}
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
        state = dialog_state(record)
        dead: list[str] = []
        patch: dict[str, Any] = {}
        running = state.get(RUNNING)
        if (
            isinstance(running, dict)
            and running.get("task") not in self._turns
            and not _lease_live(record)
            and _stale(running.get("at"))
        ):
            dead.append(str(running.get("task")))
            patch[RUNNING] = None
        for task_id, entry in _queue(state):
            if task_id not in self._waiting and _stale(entry.get("seen")):
                dead.append(task_id)
                patch[QUEUE + task_id] = None
        if not dead:
            return 0
        await self._threads.update_frontend(record.key, FRONTEND, patch)
        index = task_index(record)
        for task_id in dead:
            await self._ainterrupt(record.key, index.get(task_id))
        return len(dead)

    async def _ainterrupt(self, key: ThreadKey, position: Any) -> None:
        task = await self._tasks.at(key, position)
        if task is None or task.status.state in FINAL_STATES:
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
        cwd = dialog_state(
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
