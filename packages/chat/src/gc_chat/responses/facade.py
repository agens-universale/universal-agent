"""Сессии Responses: один агент на тред, один ход за раз."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from collections import OrderedDict
from typing import Any

from gc_core.settings import LoadedSettings
from gc_core.thread_paths import resolve_threads_dir
from gc_core.thread_store import validate_thread_id

from gc_chat.agent_config import prepare_agent_settings
from gc_chat.responses.ask_human import bind_ask_human
from gc_chat.responses.errors import ApiError
from gc_chat.responses.index_store import (
    SYSTEM,
    ResponseIndex,
    ResponseRecord,
    thread_key,
)
from gc_chat.responses.input_text import (
    IncomingTurn,
    cwd_for_continue,
    cwd_for_new,
    parse_turn,
)
from gc_chat.responses.prompt_assets import (
    responses_special_instructions_path,
)
from gc_chat.responses.stream_map import StreamMap
from gc_chat.responses.tool_flow import ToolFlow
from gc_chat.responses.turn import TurnRun

logger = logging.getLogger(__name__)

_NOT_FOUND = "not_found"
_RESPONSE_NOT_FOUND = "response not found"
_CONFLICT = "conflict"
# Живых агентов на процесс: у каждого открыт run-лог и MCP-сессии. Вытесняется
# самый давний простаивающий; продолжение диалога поднимет его из файла треда.
# ponytail: фиксированный потолок, в конфиг — если 32 параллельных чата мало.
MAX_LIVE_AGENTS = 32


class ResponsesFacade:
    def __init__(
        self,
        *,
        settings: LoadedSettings,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
        index: ResponseIndex | None = None,
    ) -> None:
        self._settings = settings
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._index = index or ResponseIndex.for_threads_dir(
            resolve_threads_dir(settings)
        )
        chat_api = settings.settings.chat_api
        self.auth_token = chat_api.auth_token
        self.cors_origins = _cors_origins(chat_api.cors_origins)
        self.bind_host = chat_api.host
        self.model_id = str(settings.settings.agent.model or "")
        self._agents: OrderedDict[str, Any] = OrderedDict()
        self._busy: set[str] = set()
        self._live: dict[str, TurnRun] = {}
        self._gate = asyncio.Lock()
        self._create_lock = asyncio.Lock()

    async def open(self, body: Any) -> TurnRun:
        incoming = parse_turn(body)
        thread_id, cwd = await self._reserve(incoming)
        try:
            agent = await self._agent_for(thread_id, cwd)
        except Exception:
            self._busy.discard(thread_id)
            logger.exception("responses agent")
            raise
        return await self._begin(incoming, thread_id, cwd, agent)

    def new_dialog_cwd(self, sent: str | None = None) -> str:
        """Корень нового хода: абсолютный путь или launch_root."""
        return cwd_for_new(sent, self._settings.settings.chat_api.launch_root)

    async def get(self, response_id: str) -> dict:
        record = await self._index.load(response_id)
        if record is None:
            raise ApiError(404, _NOT_FOUND, _RESPONSE_NOT_FOUND)
        return record.response

    async def cancel(self, response_id: str) -> dict:
        record = await self._index.load(response_id)
        live = self._live.get(response_id)
        if record is None and live is None:
            raise ApiError(404, _NOT_FOUND, _RESPONSE_NOT_FOUND)
        if live is None:
            return record.response if record is not None else {}
        await live.request_stop()
        await live.done.wait()
        return await live.result()

    def turn_busy(self, thread_id: str) -> bool:
        return thread_id in self._busy

    async def shutdown(self) -> None:
        runs = list(self._live.values())
        for run in runs:
            await run.request_stop()
        for run in runs:
            await run.done.wait()
        for agent in list(self._agents.values()):
            await _safe_aclose(agent)
        self._agents.clear()

    async def _reserve(self, incoming: IncomingTurn) -> tuple[str, str]:
        async with self._gate:
            if incoming.previous_response_id is None:
                thread_id = validate_thread_id(str(uuid.uuid4()))
                cwd = self.new_dialog_cwd(incoming.cwd_sent)
            else:
                record = await self._index.load(incoming.previous_response_id)
                if record is None:
                    raise ApiError(404, _NOT_FOUND, _RESPONSE_NOT_FOUND)
                thread_id = record.thread_id
                cwd = cwd_for_continue(incoming.cwd_sent, record.cwd)
            if thread_id in self._busy:
                raise ApiError(409, _CONFLICT, "turn already running")
            self._busy.add(thread_id)
            return thread_id, cwd

    async def _agent_for(self, thread_id: str, cwd: str) -> Any:
        cached = self._agents.get(thread_id)
        if cached is not None:
            self._agents.move_to_end(thread_id)
            return cached
        async with self._create_lock:
            cached = self._agents.get(thread_id)
            if cached is not None:
                self._agents.move_to_end(thread_id)
                return cached
            settings = prepare_agent_settings(
                self._settings,
                cwd=cwd,
                instructions_path=responses_special_instructions_path(),
            )
            agent = await self._agent_factory(
                settings,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )
            try:
                bind_ask_human(agent)
            except Exception:
                await _safe_aclose(agent)
                raise
            self._agents[thread_id] = agent
            await self._evict_idle()
            return agent

    async def _evict_idle(self) -> None:
        """Закрыть самых давних простаивающих агентов сверх ``MAX_LIVE_AGENTS``.
        Агент с идущим ходом не трогается."""
        idle = [tid for tid in self._agents if tid not in self._busy]
        for thread_id in idle[: max(0, len(self._agents) - MAX_LIVE_AGENTS)]:
            await _safe_aclose(self._agents.pop(thread_id))

    async def _begin(
        self, incoming: IncomingTurn, thread_id: str, cwd: str, agent: Any
    ) -> TurnRun:
        response_id = "resp_" + str(uuid.uuid4())
        created_at = int(time.time())
        mapper = StreamMap(
            response_id=response_id,
            model=incoming.model,
            previous_response_id=incoming.previous_response_id,
            metadata=incoming.metadata,
            created_at=created_at,
        )
        record = ResponseRecord(
            response_id,
            thread_id,
            cwd,
            incoming.previous_response_id,
            created_at,
            "in_progress",
            mapper.snapshot(),
        )
        try:
            await self._index.save(record)
            flow = await ToolFlow.start(
                self._index.threads, thread_key(thread_id)
            )
        except Exception:
            self._busy.discard(thread_id)
            raise
        run = TurnRun(
            agent=agent,
            mapper=mapper,
            flow=flow,
            index=self._index,
            record=record,
            prompt=incoming.text,
            thread_id=thread_id,
            release_busy=self._release,
        )
        self._live[response_id] = run
        run.start()
        return run

    def _release(self, thread_id: str, response_id: str) -> None:
        self._busy.discard(thread_id)
        self._live.pop(response_id, None)


def _cors_origins(raw: list[str]) -> list[str]:
    cors_origins = [
        item for item in raw if isinstance(item, str) and item not in {"", "*"}
    ]
    return cors_origins


async def _safe_aclose(agent: Any) -> None:
    close = getattr(agent, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("responses aclose")


# system id ядра — тот же, что у индекса. Импорт для вызывающих тестов.
RESPONSES_SYSTEM = SYSTEM
