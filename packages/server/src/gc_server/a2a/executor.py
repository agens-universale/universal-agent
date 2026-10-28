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
import uuid
from collections import OrderedDict
from typing import Any

from a2a.helpers.proto_helpers import new_task_from_user_message
from a2a.server.agent_execution import AgentExecutor, RequestContext
from a2a.server.tasks import TaskUpdater
from a2a.types import a2a_pb2 as pb
from gc_core.execution.environment import utc_now
from gc_core.threads.errors import OperationMismatch
from gc_core.threads.facade import Threads
from gc_core.threads.instance import instance_id, session_owner
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.threads.rules import lease_expired
from gc_core.tools.turn.turn_close import close_view, closing_reply_of
from google.protobuf.json_format import MessageToDict

from gc_server import owners
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
#: Последний захват диалога (ход задачи): ``op_id`` следующего захвата —
#: от него, так из инстансов, прочитавших один снимок, диалог берёт один.
_CLAIM_KEY = "claim"
#: Как часто ждущая в очереди задача проверяет диалог.
QUEUE_POLL_SECONDS = 2.0
#: Сколько ждать, пока SDK сохранит итог задачи (его пишет фон SDK), прежде
#: чем снять отметку хода: 200 × 0,05 с = 10 с.
SETTLE_POLLS = 200
SETTLE_POLL_SECONDS = 0.05


def entry_id(context_id: str, task_id: str) -> str:
    """Запись задачи в реестре владельца (``gc_server.owners``)."""
    return f"{context_id}|{task_id}"


def answer_text(agent: Any) -> str:
    last = getattr(agent, "last_answer", None)
    reply = closing_reply_of(agent)
    # Ход, закрытый планом или turn_finish: пиру — текст плана или итог.
    # Одобрения в A2A нет: согласие пира — не согласие пользователя.
    close = close_view(last, reply)
    if close is not None:
        return close.render(markdown=False)
    # Проза и прочие значения остаются как есть.
    if last is None:
        return ""
    if isinstance(last, str):
        return last
    return str(last)


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


def _dead_marks(
    state: dict[str, Any], owner_key: str, task_id: str
) -> dict[str, Any]:
    """Отметки задачи, поставленные мёртвым владельцем: снять."""
    patch: dict[str, Any] = {}
    running = state.get(RUNNING)
    if (
        isinstance(running, dict)
        and running.get("task") == task_id
        and running.get("owner") == owner_key
    ):
        patch[RUNNING] = None
    queued = state.get(QUEUE + task_id)
    if isinstance(queued, dict) and queued.get("owner") == owner_key:
        patch[QUEUE + task_id] = None
    return patch


def _held_elsewhere(
    state: dict[str, Any], owner_key: str, task_id: str
) -> bool:
    """Задачу ведёт другой владелец (ответ на вопрос пришёл на другой
    инстанс): устаревшая запись её не трогает."""
    running = state.get(RUNNING)
    queued = state.get(QUEUE + task_id)
    return (
        isinstance(running, dict)
        and running.get("task") == task_id
        and running.get("owner") != owner_key
    ) or (isinstance(queued, dict) and queued.get("owner") != owner_key)


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
        #: Реестр владельца: задачи «в работе» этого процесса и его пульс —
        #: по нему восстановление находит задачи упавшего инстанса.
        self._owners = owners.OwnerRegistry(
            threads, section=FRONTEND, on_lost=self._aon_owners_lost
        )
        #: Задачи этого процесса в реестре: запись → ``{ctx, task, owner}``
        #: (``owner`` — ключ реестра; ``None``, пока запись пишется). Запись
        #: реестра без хода здесь разбирает самосверка (``arecover_once``).
        self._inflight: dict[str, dict[str, Any]] = {}
        #: Задачи этого инстанса, ждущие очереди диалога: запись очереди.
        self._waiting: dict[str, dict[str, Any]] = {}
        self._stopping = False
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
            # Проверка cwd и принятие в очередь — одна условная запись.
            await self._aenqueue(context.context_id, context.task_id, cwd)
        except _CwdError:
            cwd = None
        try:
            if task is None:
                await event_queue.enqueue_event(
                    new_task_from_user_message(context.message)
                )
            if cwd is None:
                await updater.failed(_agent_message(updater, CWD_TEXT))
            else:
                await self._abegin(context, updater, cwd)
        except BaseException:
            # Запрос снят до уборки (отмена SDK во время старта агента,
            # сбой хранилища): свои отметки — снять, диалог не держать.
            await asyncio.shield(
                self._aabandon(context.context_id, context.task_id)
            )
            raise
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
        # Ожидание ответа, место в очереди и ход на другом инстансе снимает
        # снимок ``CANCELED`` (``ThreadTaskStore.save``) — так же, как
        # отмену, которую SDK пишет мимо исполнителя.
        await updater.cancel()
        if turn is not None:
            # Запрос задачи (с ``_report``) SDK при отмене снимает: место в
            # диалоге освобождаем сами — очередь идёт сразу. Сбой уборки
            # отмену не срывает: запись разберёт самосверка.
            try:
                await self._afree(context_id, context.task_id)
                await self._arelease_entry(context_id, context.task_id)
            except Exception:
                logger.warning("a2a: уборка после отмены", exc_info=True)

    async def shutdown(self) -> None:
        # Остановка сервера — прерывание: задачи остаются как есть, их
        # подберёт восстановление другого инстанса. Ждущие очереди диалог
        # больше не занимают.
        self._stopping = True
        self._waiting.clear()
        for turn in list(self._turns.values()):
            turn.interrupted = True
            await turn.cancel()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()
        # Аренда реестра — отпустить: задачи разберут сразу, не через срок.
        await self._owners.astop()

    async def _aon_owners_lost(self) -> None:
        """Свой реестр потерян (продление опоздало: его взял или уже
        разобрал восстановитель). Ходы сняты, ждущие очереди перестают
        ждать, а свои задачи процесс разбирает сам, как восстановитель:
        реестр могли удалить раньше, чем в него легла последняя запись."""
        logger.warning("a2a: реестр владельца потерян, свои задачи сняты")
        lost = [e for e in self._inflight.values() if e["owner"] is not None]
        self._inflight.clear()
        self._waiting.clear()
        for turn in list(self._turns.values()):
            turn.interrupted = True
            await turn.cancel()
        for entry in lost:
            try:
                await self._arecover_entry(entry["owner"], entry)
            except Exception:
                logger.warning("a2a: разбор своей задачи", exc_info=True)

    async def _aregister(self, context_id: str, task_id: str) -> str:
        """Запись в реестр — до любой отметки в диалоге; ключ реестра —
        владелец отметок задачи."""
        name = entry_id(context_id, task_id)
        self._inflight[name] = {
            "ctx": context_id,
            "task": task_id,
            "owner": None,
        }
        try:
            owner = await self._owners.aadd(
                name, {"ctx": context_id, "task": task_id}
            )
        except BaseException:
            self._inflight.pop(name, None)
            raise
        self._inflight[name] = {
            "ctx": context_id,
            "task": task_id,
            "owner": owner,
        }
        return owner

    def _owner_of(self, context_id: str, task_id: str) -> str | None:
        entry = self._inflight.get(entry_id(context_id, task_id))
        return None if entry is None else entry["owner"]

    async def _arelease_entry(self, context_id: str, task_id: str) -> None:
        """Снять запись реестра — последней. Реестр уже не наш — нечего
        снимать; сбой — запись разберёт самосверка."""
        name = entry_id(context_id, task_id)
        entry = self._inflight.pop(name, None)
        if entry is None or entry["owner"] != self._owners.key:
            return
        try:
            await self._owners.aremove(name)
        except Exception:
            logger.warning("a2a: запись реестра не снята", exc_info=True)

    async def _aabandon(self, context_id: str, task_id: str) -> None:
        """Ход ушёл без уборки: свои отметки — снять сразу (диалог
        свободен), запись реестра — оставить самосверке (итог к тому времени
        запишет отмена; нет итога — FAILED)."""
        name = entry_id(context_id, task_id)
        entry = self._inflight.pop(name, None)
        if self._stopping or entry is None or entry["owner"] is None:
            return
        key = dialog_key(context_id)
        try:
            state = dialog_state(await self._threads.get(key))
            patch = _dead_marks(state, entry["owner"], task_id)
            if patch:
                await self._threads.update_frontend(key, FRONTEND, patch)
        except Exception:
            logger.warning("a2a: отметки брошенного хода", exc_info=True)

    async def _abegin(
        self, context: RequestContext, updater: TaskUpdater, cwd: str
    ):
        task_id, context_id = context.task_id, context.context_id
        if not await self._aawait_turn(context_id, task_id):
            # Отменена в очереди (итог записан отменой) — запись снять;
            # остановка сервера — оставить восстановлению.
            if not self._stopping:
                await self._arelease_entry(context_id, task_id)
            return
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
        # Запись в реестре — до отметки хода: умрёт процесс — задачу найдут.
        owner = await self._aregister(context_id, task_id)
        try:
            if not await self._aclaim_dialog(
                key, state, task_id, owner, {WAITING: None}
            ):
                # Одновременный ответ с другого инстанса взял диалог раньше.
                await self._arelease_entry(context_id, task_id)
                return
            await self._arun_claimed(
                updater,
                task_id,
                context_id,
                state.get(_CWD_KEY),
                [],
                answer=text,
            )
        except BaseException:
            await asyncio.shield(self._aabandon(context_id, task_id))
            raise

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
        """Ход задачи, занявшей диалог: агент, ход, итог. Жива ли задача,
        говорит реестр владельца (его аренда), а не пульс отметки."""
        owner = self._owner_of(context_id, task_id)
        try:
            agent = await self._agent_for(context_id, cwd)
        except Exception:
            logger.exception("a2a agent")
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
            await self._afinish(context_id, task_id, FINAL_STATES)
            return
        if owner is None or self._owner_of(context_id, task_id) != owner:
            return  # реестр потерян, пока создавался агент: задача разобрана
        await updater.start_work()
        turn = Turn(agent, task_id, context_id, prompt, answer=answer)
        self._turns[task_id] = turn
        turn.start()
        await turn.done.wait()
        await self._report(turn, updater)

    async def _aenqueue(self, context_id: str, task_id: str, cwd: str):
        """Встать в очередь диалога (порядок — по времени приёма). Запись в
        реестре владельца — до записи очереди: умрёт процесс — задачу
        найдут по реестру."""
        owner = await self._aregister(context_id, task_id)
        entry = {"at": _now(), "instance": instance_id(), "owner": owner}
        from gc_core.threads.errors import ThreadConflict
        from gc_core.threads.models import SideChange

        key = dialog_key(context_id)
        try:
            while True:
                record = await self._threads.get(key)
                current = dialog_state(record).get(_CWD_KEY)
                if current is not None and current != cwd:
                    raise _CwdError(cwd)
                try:
                    await self._threads.store.append_side(
                        key,
                        SideChange(
                            op_id=f"a2a-enqueue-{uuid.uuid4().hex}",
                            expected_frontend=record.frontend
                            if record
                            else {},
                            patch_frontend=(
                                FRONTEND,
                                {_CWD_KEY: cwd, QUEUE + task_id: entry},
                            ),
                        ),
                    )
                    break
                except ThreadConflict:
                    continue
        except BaseException:
            await self._arelease_entry(context_id, task_id)
            raise
        self._waiting[task_id] = entry

    async def _aawait_turn(self, context_id: str, task_id: str) -> bool:
        """Дождаться своей очереди: диалог свободен (нет хода, никто не
        ждёт ответа) и задача первая. ``False`` — задачу сняли: отменили в
        очереди или (реестр потерян) её разберёт восстановление."""
        key = dialog_key(context_id)
        try:
            while task_id in self._waiting:
                record = await self._threads.get(key)
                state = dialog_state(record)
                queued = state.get(QUEUE + task_id)
                if state.get(CANCELED + task_id) or not isinstance(
                    queued, dict
                ):
                    return False
                owner = queued.get("owner")
                if owner != self._owners.key:
                    return False  # реестр потерян: задачу разберут
                if (
                    self._dialog_free(record, state)
                    and _queue(state)[:1] == [(task_id, queued)]
                    and await self._aclaim_dialog(
                        key, state, task_id, owner, {QUEUE + task_id: None}
                    )
                ):
                    return True
                await asyncio.sleep(QUEUE_POLL_SECONDS)
            return False
        finally:
            self._waiting.pop(task_id, None)

    @staticmethod
    def _claim(task_id: str, owner: str) -> dict[str, Any]:
        """Отметка хода: владелец — ключ реестра, куда легла запись задачи
        (а не текущий: реестр мог смениться между записью и захватом)."""
        return {
            "task": task_id,
            "instance": instance_id(),
            "owner": owner,
            "at": _now(),
        }

    async def _aclaim_dialog(
        self,
        key: ThreadKey,
        state: dict[str, Any],
        task_id: str,
        owner: str,
        patch: dict[str, Any],
    ) -> bool:
        """Занять диалог ходом задачи. Инстансы, прочитавшие тот же снимок
        (голова очереди разошлась, два ответа на вопрос), пишут одну
        операцию с разными захватами: хранилище примет первую, остальным —
        ``OperationMismatch``, и они хода не начинают."""
        try:
            await self._threads.update_frontend(
                key,
                FRONTEND,
                {
                    **patch,
                    RUNNING: self._claim(task_id, owner),
                    _CLAIM_KEY: uuid.uuid4().hex,
                },
                op_id=f"a2a-claim:{state.get(_CLAIM_KEY)}",
            )
        except OperationMismatch:
            return False
        return True

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
        держит очередь диалога), иначе итог задачи. Отметки и запись реестра
        снимаются, когда итог уже в хранилище: упади процесс раньше —
        восстановление сверит диалог."""
        await turn.done.wait()
        self._turns.pop(turn.task_id, None)
        if turn.interrupted:
            return  # отметка и запись остаются: задачу прервёт восстановление
        if turn.cancel_requested or await self._afinished(turn.task_id):
            # Итог уже записан: cancel() или отмена мимо исполнителя.
            await self._afree(turn.context_id, turn.task_id)
            await self._arelease_entry(turn.context_id, turn.task_id)
            return
        question = getattr(turn.agent, "pending_question", None)
        if turn.failed is None and question:
            # Отметка ожидания — до события пиру: его ответ может прийти
            # сразу и должен её застать.
            await self._threads.update_frontend(
                dialog_key(turn.context_id),
                FRONTEND,
                {RUNNING: None, WAITING: turn.task_id},
            )
            await updater.requires_input(
                _agent_message(updater, str(question.get("question") or ""))
            )
            await self._afinish(
                turn.context_id,
                turn.task_id,
                {pb.TASK_STATE_INPUT_REQUIRED},
                free=False,
            )
            return
        if turn.failed is not None or stop_outcome(turn.agent) is not None:
            await updater.failed(_agent_message(updater, PEER_FAILURE_TEXT))
        else:
            await updater.add_artifact(
                [pb.Part(text=answer_text(turn.agent))], name="answer"
            )
            await updater.complete()
        await self._afinish(turn.context_id, turn.task_id, FINAL_STATES)

    async def _afinish(
        self,
        context_id: str,
        task_id: str,
        states: Any,
        *,
        free: bool = True,
    ) -> None:
        """Итог задачи пишет SDK в фоне — дождаться, пока он в хранилище, и
        только тогда снять отметку хода и запись реестра. Не дождались —
        запись остаётся: её разберёт самосверка."""
        if not await self._asettled(task_id, states):
            logger.warning("a2a: итог задачи %s не сохранён вовремя", task_id)
            self._inflight.pop(entry_id(context_id, task_id), None)
            return
        if free:
            await self._afree(context_id, task_id)
        await self._arelease_entry(context_id, task_id)

    async def _asettled(self, task_id: str, states: Any) -> bool:
        for _ in range(SETTLE_POLLS):
            task = await self._tasks.latest(task_id)
            if task is not None and task.status.state in states:
                return True
            await asyncio.sleep(SETTLE_POLL_SECONDS)
        return False

    # --- восстановление ------------------------------------------------

    async def arecover_once(self) -> int:
        """Задачи упавших инстансов — ``FAILED`` (прерваны), их место в
        диалоге освобождается. Читаются каталог реестров владельцев и
        записи мёртвых, а не все диалоги. Возвращает, сколько задач
        прервано."""
        failed = 0
        own = self._owners.key
        if own is not None:
            failed += await self._areconcile_own(own)
        for key in await owners.adead(self._threads, own=own, now=utc_now()):
            record = await owners.aclaim_dead(
                self._threads,
                key,
                owner=f"recover-{own or session_owner()}",
                ttl=self._owners.ttl,
            )
            if record is not None:  # его уже разбирает другой инстанс
                failed += await self._arecover_owner(key, record)
        return failed

    async def _areconcile_own(self, own: str) -> int:
        """Самосверка: записи своего реестра, за которыми нет хода в памяти
        (уборка не прошла — сбой хранилища, снятый запрос), разобрать, как
        записи мёртвого: иначе отметка жила бы до рестарта процесса."""
        record = await self._threads.get(owners.registry_key(own))
        failed = 0
        for name, entry in owners.entries(record, FRONTEND).items():
            if name in self._inflight:
                continue
            outcome = await self._arecover_entry(own, entry)
            if outcome is None:
                continue
            failed += outcome
            await self._owners.aremove(name)
        return failed

    async def _arecover_owner(self, key: str, record: ThreadRecord) -> int:
        """Разобрать мёртвый реестр (взятый своей арендой): задачи, чей
        ход ещё держит тред диалога, ждут следующего прохода."""
        failed, resolved, kept = 0, [], False
        for name, entry in owners.entries(record, FRONTEND).items():
            outcome = await self._arecover_entry(key, entry)
            if outcome is None:
                kept = True
                continue
            resolved.append(name)
            failed += outcome
        if kept:
            await owners.aforget(
                self._threads, key, section=FRONTEND, entry_ids=resolved
            )
        else:
            await owners.adrop(self._threads, key)
        return failed

    async def _arecover_entry(
        self, owner_key: str, entry: dict[str, Any]
    ) -> int | None:
        """Сверить задачу владельца с диалогом: снять его отметки и прервать
        задачу без итога. ``None`` — аренда треда диалога жива (ход ещё
        пишет): следующий проход. Задачу ведёт другой — не трогать."""
        task_id = str(entry.get("task"))
        key = dialog_key(str(entry.get("ctx")))
        record = await self._threads.get(key)
        state = dialog_state(record)
        if _held_elsewhere(state, owner_key, task_id):
            return 0
        patch = _dead_marks(state, owner_key, task_id)
        if RUNNING in patch and _lease_live(record):
            return None
        position = task_index(record).get(task_id)
        asked = await self._asked(key, position)
        if state.get(WAITING) == task_id:
            if asked:
                return 0  # ждёт ответа пира — процесса не держит
            patch[WAITING] = None  # умер между отметкой и событием пиру
        if patch:
            await self._threads.update_frontend(key, FRONTEND, patch)
        elif asked:
            return 0
        # Без отметок — тоже: задача не начнётся (умер до отметки).
        return int(await self._ainterrupt(key, position))

    async def _asked(self, key: ThreadKey, position: Any) -> bool:
        task = await self._tasks.at(key, position)
        return (
            task is not None
            and task.status.state == pb.TASK_STATE_INPUT_REQUIRED
        )

    async def _ainterrupt(self, key: ThreadKey, position: Any) -> bool:
        task = await self._tasks.at(key, position)
        if task is None or task.status.state in FINAL_STATES:
            return False
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
        return True

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
