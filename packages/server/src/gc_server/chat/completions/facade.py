"""Один запрос — новый агент, новый тред, после ответа тред забыт."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from gc_core.settings import LoadedSettings
from gc_core.thread_store import validate_thread_id
from gc_core.threads.facade import Threads

from gc_server.chat.agent_config import prepare_agent_settings
from gc_server.chat.completions.ask_human import bind_ask_human
from gc_server.chat.completions.content import ContentMap
from gc_server.chat.completions.history import (
    forget_thread,
    seed_prior,
    thread_key,
)
from gc_server.chat.completions.input_text import (
    IncomingTurn,
    parse_turn,
)
from gc_server.chat.completions.prompt_assets import (
    completions_special_instructions_path,
)
from gc_server.chat.completions.turn import TurnRun, _safe_aclose
from gc_server.chat.cwd import cwd_for_new
from gc_server.chat.tool_flow import ToolFlow
from gc_server.config import LOOPBACK, ChatConfig

logger = logging.getLogger(__name__)


class CompletionsFacade:
    def __init__(
        self,
        *,
        settings: LoadedSettings,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
        section: ChatConfig | None = None,
    ) -> None:
        section = section or ChatConfig(host=LOOPBACK)
        self._settings = settings
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._threads = Threads.from_settings(settings)
        self.auth_token = section.auth_token or ""
        self.cors_origins = _cors_origins(section.cors_origins)
        self.bind_host = section.host or LOOPBACK
        self._launch_root = section.launch_root
        self.model_id = str(settings.settings.agent.model or "")
        self._create_lock = asyncio.Lock()

    async def open(self, body: Any) -> TurnRun:
        incoming = parse_turn(body)
        thread_id = validate_thread_id(str(uuid.uuid4()))
        cwd = cwd_for_new(incoming.cwd_sent, self._launch_root)
        agent = await self._agent_for(thread_id, cwd)
        try:
            bind_ask_human(agent)
            await seed_prior(self._threads, thread_id, incoming.prior)
        except Exception:
            logger.exception("completions seed")
            await _safe_aclose(agent)
            await _forget_quietly(self._threads, thread_id)
            raise
        return await self._begin(incoming, thread_id, agent)

    async def _agent_for(self, thread_id: str, cwd: str) -> Any:
        del thread_id
        async with self._create_lock:
            settings = prepare_agent_settings(
                self._settings,
                cwd=cwd,
                instructions_path=completions_special_instructions_path(),
            )
            return await self._agent_factory(
                settings,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )

    async def _begin(
        self, incoming: IncomingTurn, thread_id: str, agent: Any
    ) -> TurnRun:
        run = TurnRun(
            agent=agent,
            content=ContentMap(),
            flow=await ToolFlow.start(self._threads, thread_key(thread_id)),
            prompt=incoming.prompt,
            thread_id=thread_id,
            threads=self._threads,
            completion_id="chatcmpl_" + str(uuid.uuid4()),
            created=int(time.time()),
            model=incoming.model,
        )
        run.start()
        return run


async def _forget_quietly(threads: Threads, thread_id: str) -> None:
    try:
        await forget_thread(threads, thread_id)
    except Exception:
        logger.exception("completions delete thread")


def _cors_origins(raw: list[str]) -> list[str]:
    return [
        item for item in raw if isinstance(item, str) and item not in {"", "*"}
    ]
