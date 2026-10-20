"""Исполнитель задач A2A для SDK: диалог — тред ``system="a2a"``
(``contextId`` — id треда), ход агента — в фоне.

Вопрос агента человеку (``turn_ask_human``) становится вопросом задачи:
``INPUT_REQUIRED``, ``execute`` возвращается, а ход ждёт ответа. Следующее
сообщение с тем же ``taskId`` снова вызывает ``execute`` — ответ уходит в
ждущий ход. Ход живёт на инстансе, который его начал: продолжение,
пришедшее на другой инстанс, — отказ задачи.
"""

from __future__ import annotations

import asyncio
import logging
import os
from collections import OrderedDict
from typing import Any

from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2 as pb
from gc_core.execution.human import HumanRequestInterrupted
from gc_core.threads.facade import Threads
from gc_core.tools.turn.propose_plan import ProposePlan
from gc_core.tools.turn.submit_solution import SubmitSolution
from google.protobuf.json_format import MessageToDict

from gc_server.a2a.agent_config import prepare_agent_settings
from gc_server.a2a.ask_human import bind_ask_human
from gc_server.a2a.task_store import SYSTEM, dialog_key

logger = logging.getLogger(__name__)

# Живых агентов на процесс, как у Responses. Вытесняется самый давний
# простаивающий; ход в работе не закрывается.
MAX_LIVE_AGENTS = 32
# Пиру в status.message — без путей и текста исключения ядра.
PEER_FAILURE_TEXT = "Ход не выполнен."
BUSY_TEXT = "Диалог занят: в нём идёт другая задача."
ELSEWHERE_TEXT = (
    "Ход этой задачи идёт на другом инстансе или прерван: ответ на вопрос "
    "принять некому."
)
CWD_TEXT = (
    "metadata.cwd должен быть абсолютным путём внутри корня агента и "
    "совпадать с диалогом."
)
# Досрочная остановка — не ответ: бюджет (шаги, токены, тулы) и недоступная
# проверка действий судьёй. JSON ``ok: false`` в артефакт не кладём.
_STOPS = frozenset(
    {"step_limit", "token_limit", "tool_call_limit", "judge_unavailable"}
)
#: Корень диалога в состоянии фронтенда ``a2a`` треда.
_CWD_KEY = "cwd"


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


class Turn:
    """Ход задачи в фоне. Исходы — в очередь: вопрос пиру или конец."""

    def __init__(self, agent: Any, task_id: str, context_id: str, prompt: str):
        self.agent = agent
        self.task_id = task_id
        self.context_id = context_id
        self._prompt = prompt
        self._outcomes: asyncio.Queue = asyncio.Queue()
        self._answer: asyncio.Future | None = None
        self.cancel_requested = False
        self.done = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def ask(self, question: str) -> str:
        if self.cancel_requested:
            raise HumanRequestInterrupted()
        self._answer = asyncio.get_running_loop().create_future()
        await self._outcomes.put(("question", question))
        try:
            return await self._answer
        except asyncio.CancelledError as exc:
            raise HumanRequestInterrupted() from exc

    def answer(self, text: str) -> None:
        if self._answer is not None and not self._answer.done():
            self._answer.set_result(text)

    async def cancel(self) -> None:
        self.cancel_requested = True
        if self._answer is not None and not self._answer.done():
            self._answer.cancel()
        stop = getattr(self.agent, "stop", None)
        if stop is not None:
            stop()
        await self.done.wait()

    async def next_outcome(self) -> tuple[str, Any]:
        return await self._outcomes.get()

    async def _run(self) -> None:
        failed: BaseException | None = None
        try:
            await self._forward()
        except asyncio.CancelledError:
            self.cancel_requested = True
        except Exception as exc:
            if not self.cancel_requested:
                failed = exc
                logger.exception("a2a turn")
        await self._outcomes.put(("done", failed))
        self.done.set()

    async def _forward(self) -> None:
        stream = self.agent.stream(
            self._prompt, thread_id=self.context_id, system=SYSTEM
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
        self._launch_root = launch_root
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._turns: dict[str, Turn] = {}
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._create_lock = asyncio.Lock()

    async def execute(self, context: RequestContext, event_queue: Any) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        turn = self._turns.get(context.task_id)
        if turn is not None:
            turn.answer(context.get_user_input())
            await self._report(turn, updater)
            return
        task = context.current_task
        if task is None:
            await event_queue.enqueue_event(
                new_task_from_user_message(context.message)
            )
        elif task.status.state == pb.TASK_STATE_INPUT_REQUIRED:
            await updater.failed(_agent_message(updater, ELSEWHERE_TEXT))
            return
        await self._begin(context, updater)

    async def cancel(self, context: RequestContext, event_queue: Any) -> None:
        updater = TaskUpdater(event_queue, context.task_id, context.context_id)
        turn = self._turns.get(context.task_id)
        if turn is not None:
            await turn.cancel()
        else:
            # Ход на другом инстансе: отмена — через поток control треда.
            await self._threads.request_cancel(dialog_key(context.context_id))
        await updater.cancel()

    async def shutdown(self) -> None:
        for turn in list(self._turns.values()):
            await turn.cancel()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()

    async def _begin(self, context: RequestContext, updater: TaskUpdater):
        try:
            cwd = await self._cwd_for(context)
        except _CwdError:
            await updater.failed(_agent_message(updater, CWD_TEXT))
            return
        if self._busy(context.context_id) or await self._threads.busy_owner(
            dialog_key(context.context_id)
        ):
            await updater.failed(_agent_message(updater, BUSY_TEXT))
            return
        try:
            agent = await self._agent_for(context.context_id, cwd)
        except Exception:
            logger.exception("a2a agent")
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            return
        turn = Turn(
            agent,
            context.task_id,
            context.context_id,
            context.get_user_input(),
        )
        bind_ask_human(agent, turn.ask)
        self._turns[context.task_id] = turn
        await self._threads.update_frontend(
            dialog_key(context.context_id), "a2a", {_CWD_KEY: cwd}
        )
        await updater.start_work()
        turn.start()
        await self._report(turn, updater)

    async def _report(self, turn: Turn, updater: TaskUpdater) -> None:
        """Ждать исхода хода: вопрос — ``INPUT_REQUIRED`` (ход ждёт ответа),
        конец — итог задачи."""
        kind, value = await turn.next_outcome()
        if kind == "question":
            await updater.requires_input(_agent_message(updater, value))
            return
        self._turns.pop(turn.task_id, None)
        if turn.cancel_requested:
            return  # итог — cancel()
        if value is not None or stop_outcome(turn.agent) is not None:
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            return
        await updater.add_artifact(
            [pb.Part(text=answer_text(turn.agent))], name="answer"
        )
        await updater.complete()

    def _busy(self, context_id: str) -> bool:
        return any(t.context_id == context_id for t in self._turns.values())

    async def _cwd_for(self, context: RequestContext) -> str:
        sent = _sent_cwd(context)
        known = await self._dialog_cwd(context.context_id)
        if known is not None:
            if sent is not None and os.path.normpath(sent) != known:
                raise _CwdError(sent)
            return known
        root = self._root()
        if sent is None:
            return root
        if not os.path.isabs(sent):
            raise _CwdError(sent)
        # Корень хода — периметр записи агента: пир выбирает его только
        # внутри корня поверхности (симлинки и «..» раскрыты).
        cwd = os.path.realpath(sent)
        if os.path.commonpath([os.path.realpath(root), cwd]) != (
            os.path.realpath(root)
        ):
            raise _CwdError(sent)
        return os.path.normpath(sent)

    def _root(self) -> str:
        """Корень поверхности: a2a.launch_root или каталог процесса."""
        root = self._launch_root
        if isinstance(root, str) and os.path.isabs(root):
            return os.path.normpath(root)
        return os.path.normpath(os.getcwd())

    async def _dialog_cwd(self, context_id: str) -> str | None:
        """Корень диалога — из прошлых задач контекста (на любом инстансе)."""
        record = await self._threads.get(dialog_key(context_id))
        state = (record.frontend.get("a2a") or {}) if record else {}
        cwd = state.get(_CWD_KEY)
        return cwd if isinstance(cwd, str) and cwd else None

    async def _agent_for(self, context_id: str, cwd: str) -> Any:
        cached = self._agents.get(context_id)
        if cached is not None:
            self._agents.move_to_end(context_id)
            return cached
        async with self._create_lock:
            cached = self._agents.get(context_id)
            if cached is not None:
                return cached
            agent = await self._agent_factory(
                prepare_agent_settings(self._settings, cwd=cwd),
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
