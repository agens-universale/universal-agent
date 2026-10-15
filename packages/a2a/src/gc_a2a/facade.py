"""Сессии A2A: один агент на диалог, один ход за раз."""

from __future__ import annotations

import asyncio
import logging
import uuid
from collections import OrderedDict
from typing import Any

from gc_core.settings import LoadedSettings

from gc_a2a.agent_config import prepare_agent_settings
from gc_a2a.ask_human import bind_ask_human, bind_confirm_deny
from gc_a2a.card import agent_card, interface_url
from gc_a2a.dialog import (
    DialogError,
    OpenNew,
    OpenTask,
    Resume,
    decide,
)
from gc_a2a.errors import (
    RpcError,
    SealError,
    internal_error,
    invalid_params,
    not_cancelable,
    task_not_found,
    unsupported,
)
from gc_a2a.listing import page
from gc_a2a.params import (
    Incoming,
    parse_get,
    parse_list,
    parse_send,
    parse_task_id,
)
from gc_a2a.store import CANCELED, TERMINAL, WORKING, TaskStore
from gc_a2a.turn import PEER_FAILURE_TEXT, Turn

logger = logging.getLogger(__name__)

# Живых агентов на процесс, как у Responses. Вытесняется самый давний
# простаивающий; ход в работе не закрывается.
MAX_LIVE_AGENTS = 32


class A2aFacade:
    def __init__(
        self,
        *,
        settings: LoadedSettings,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
        clock: Any = None,
    ) -> None:
        self._settings = settings
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        section = settings.settings.a2a
        self.auth_token = section.auth_token
        self.bind_host = section.host
        self.bind_port = section.port
        self._launch_root = section.launch_root
        self._store = TaskStore(clock)
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._busy: set[str] = set()
        self._live: dict[str, Turn] = {}
        self._gate = asyncio.Lock()
        self._create_lock = asyncio.Lock()

    def card(self) -> dict:
        section = self._settings.settings.a2a
        url = section.public_url or interface_url(section.host, section.port)
        return agent_card(
            name=section.name,
            description=section.description,
            url=url,
            secured=section.auth_token != "",
        )

    async def send_message(self, params: Any) -> dict:
        incoming = parse_send(params)
        plan = await self._reserve(incoming)
        if plan is None:
            return await self._deliver(incoming)
        return await self._perform(plan, incoming)

    async def get_task(self, params: Any) -> dict:
        task_id, history_length = parse_get(params)
        if self._store.task(task_id) is None:
            raise task_not_found()
        return await self._store.view(task_id, history_length)

    async def list_tasks(self, params: Any) -> dict:
        query = parse_list(params)
        return page(await self._store.snapshot(), query)

    async def cancel_task(self, params: Any) -> dict:
        task_id = parse_task_id(params)
        turn, done = await self._arm_cancel(task_id)
        if done:
            return await self._store.view(task_id, None)
        await turn.cancel()
        await turn.done.wait()
        return await self._store.view(task_id, None)

    async def shutdown(self) -> None:
        turns = list(self._live.values())
        for turn in turns:
            await turn.cancel()
        for turn in turns:
            await turn.done.wait()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()

    async def _reserve(self, incoming: Incoming) -> Any:
        async with self._gate:
            if self._store.message_task(incoming.message_id) is not None:
                self._require_same_request(incoming)
                return None
            try:
                decision = decide(self._store, incoming, self._launch_root)
            except DialogError as exc:
                raise _map(exc) from exc
            if isinstance(decision, Resume):
                await self._store.accept_reply(
                    decision.task_id,
                    incoming.message_id,
                    incoming.parts,
                    sent_task_id=incoming.task_id,
                    sent_context_id=incoming.context_id,
                )
                return decision
            return await self._open(decision, incoming)

    async def _open(self, decision: OpenNew | OpenTask, incoming: Incoming):
        context_id = (
            decision.context_id
            if isinstance(decision, OpenTask)
            else _new_id()
        )
        task_id = _new_id()
        await self._store.create_task(
            task_id=task_id,
            context_id=context_id,
            cwd=decision.cwd,
            message_id=incoming.message_id,
            parts=incoming.parts,
            new_dialog=isinstance(decision, OpenNew),
            sent_task_id=incoming.task_id,
            sent_context_id=incoming.context_id,
        )
        self._busy.add(context_id)
        return OpenTask(context_id, decision.cwd), task_id

    async def _perform(self, plan: Any, incoming: Incoming) -> dict:
        if isinstance(plan, Resume):
            return await self._resume(plan, incoming)
        opened, task_id = plan
        return await self._begin(opened, task_id, incoming)

    async def _resume(self, plan: Resume, incoming: Incoming) -> dict:
        turn = self._live.get(plan.task_id)
        if turn is None:
            raise internal_error()
        if incoming.return_immediately:
            view = self._store.view_now(plan.task_id, incoming.history_length)
            turn.answer(incoming.text)
            return {"task": view}
        turn.answer(incoming.text)
        return {"task": await self._wait(plan.task_id, incoming)}

    async def _begin(
        self, opened: OpenTask, task_id: str, incoming: Incoming
    ) -> dict:
        started = False
        try:
            try:
                agent = await self._agent_for(opened.context_id, opened.cwd)
                turn = self._turn(
                    agent, opened.context_id, task_id, incoming.text
                )
                bind_ask_human(agent, turn.ask)
                bind_confirm_deny(agent)
            except RpcError:
                raise
            except Exception as exc:
                logger.exception("a2a agent")
                await self._fail_setup(opened.context_id, task_id, exc)
                return await self._deliver_task(task_id, incoming)
            async with self._gate:
                task = self._store.task(task_id)
                if task is None or task.state in TERMINAL:
                    self._release(opened.context_id, task_id)
                else:
                    self._live[task_id] = turn
                    turn.start()
                    started = True
            return await self._deliver_task(task_id, incoming)
        finally:
            if not started:
                await self._abandon_setup(opened.context_id, task_id)

    async def _deliver(self, incoming: Incoming) -> dict:
        task_id = self._store.message_task(incoming.message_id)
        if task_id is None:
            raise internal_error()
        return await self._deliver_task(task_id, incoming)

    async def _deliver_task(self, task_id: str, incoming: Incoming) -> dict:
        if incoming.return_immediately:
            return {
                "task": self._store.view_now(task_id, incoming.history_length)
            }
        return {"task": await self._wait(task_id, incoming)}

    async def _wait(self, task_id: str, incoming: Incoming) -> dict:
        try:
            return await self._store.wait_view(
                task_id, incoming.history_length
            )
        except SealError as exc:
            raise internal_error() from exc

    def _turn(
        self, agent: Any, context_id: str, task_id: str, prompt: str
    ) -> Turn:
        return Turn(
            agent=agent,
            store=self._store,
            task_id=task_id,
            context_id=context_id,
            prompt=prompt,
            new_id=_new_id,
            release=self._release,
        )

    async def _agent_for(self, context_id: str, cwd: str) -> Any:
        cached = self._agents.get(context_id)
        if cached is not None:
            self._agents.move_to_end(context_id)
            return cached
        async with self._create_lock:
            cached = self._agents.get(context_id)
            if cached is not None:
                self._agents.move_to_end(context_id)
                return cached
            settings = prepare_agent_settings(self._settings, cwd=cwd)
            agent = await self._agent_factory(
                settings,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )
            self._agents[context_id] = agent
            await self._evict_idle()
            return agent

    async def _evict_idle(self) -> None:
        idle = [key for key in self._agents if key not in self._busy]
        overflow = len(self._agents) - MAX_LIVE_AGENTS
        for key in idle[: max(0, overflow)]:
            await _safe_aclose(self._agents.pop(key))

    async def _fail_setup(
        self, context_id: str, task_id: str, _exc: BaseException
    ) -> None:
        agent = self._agents.pop(context_id, None)
        if agent is not None:
            await _safe_aclose(agent)
        try:
            await self._store.mark_failed(
                task_id, _new_id(), PEER_FAILURE_TEXT
            )
        except Exception as seal_exc:
            logger.exception("a2a seal")
            await self._store.mark_broken(task_id)
            self._release(context_id, task_id)
            raise internal_error() from seal_exc
        self._release(context_id, task_id)

    async def _abandon_setup(self, context_id: str, task_id: str) -> None:
        """Снять резерв, если ход так и не стартовал (обрыв запроса и т.п.)."""
        async with self._gate:
            if task_id in self._live:
                return
            task = self._store.task(task_id)
            if task is not None and task.state == WORKING:
                await self._store.mark_canceled(task_id)
            self._release(context_id, task_id)

    async def _arm_cancel(self, task_id: str) -> tuple[Turn | None, bool]:
        async with self._gate:
            task = self._store.task(task_id)
            if task is None:
                raise task_not_found()
            if task.state == CANCELED:
                return None, True
            if task.state in TERMINAL:
                raise not_cancelable()
            turn = self._live.get(task_id)
            if turn is None:
                await self._store.mark_canceled(task_id)
                return None, True
            return turn, False

    def _require_same_request(self, incoming: Incoming) -> None:
        recorded = self._store.message_request(incoming.message_id)
        if recorded is None:
            raise invalid_params()
        sent_task_id, sent_context_id = recorded
        if incoming.task_id != sent_task_id:
            raise invalid_params()
        if incoming.context_id != sent_context_id:
            raise invalid_params()

    def _release(self, context_id: str, task_id: str) -> None:
        self._busy.discard(context_id)
        self._live.pop(task_id, None)


def _map(error: DialogError) -> RpcError:
    if error.kind == "not_found":
        return task_not_found()
    if error.kind == "unsupported":
        return unsupported()
    if error.kind == "not_cancelable":
        return not_cancelable()
    return invalid_params()


def _new_id() -> str:
    return str(uuid.uuid4())


async def _safe_aclose(agent: Any) -> None:
    close = getattr(agent, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("a2a aclose")
