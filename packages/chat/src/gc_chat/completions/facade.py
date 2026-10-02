"""Один запрос — новый агент, новый тред, после ответа тред забыт."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any

from gc_core.thread_store import resolve_threads_dir, validate_thread_id

from gc_chat.agent_config import prepare_agent_config
from gc_chat.completions.ask_human import bind_ask_human
from gc_chat.completions.content import ContentMap
from gc_chat.completions.history import (
    forget_thread,
    seed_prior,
    thread_data_path,
)
from gc_chat.completions.input_text import (
    IncomingTurn,
    cwd_for_turn,
    parse_turn,
)
from gc_chat.completions.prompt_assets import (
    completions_special_instructions_path,
)
from gc_chat.completions.tool_flow import ToolFlow
from gc_chat.completions.turn import TurnRun, _safe_aclose

logger = logging.getLogger(__name__)


class CompletionsFacade:
    def __init__(
        self,
        *,
        config: dict,
        agent_factory: Any,
        auth_connector: Any = None,
        tools_cache: Any = None,
        os_backend: Any = None,
    ) -> None:
        self._config = config
        self._agent_factory = agent_factory
        self._auth = auth_connector
        self._tools_cache = tools_cache
        self._os_backend = os_backend
        self._threads_dir = _threads_dir(config)
        self.auth_token = str(config.get("chat_api_auth_token") or "")
        self.cors_origins = _cors_origins(config)
        self.bind_host = str(config.get("chat_api_host") or "127.0.0.1")
        self.model_id = str(config.get("model") or "")
        self._create_lock = asyncio.Lock()

    async def open(self, body: Any) -> TurnRun:
        incoming = parse_turn(body)
        thread_id = validate_thread_id(str(uuid.uuid4()))
        cwd = cwd_for_turn(
            incoming.cwd_sent, self._config.get("chat_api_launch_root")
        )
        agent = await self._agent_for(thread_id, cwd)
        try:
            bind_ask_human(agent)
            seed_prior(self._threads_dir, thread_id, incoming.prior)
        except Exception:
            logger.exception("completions seed")
            await _safe_aclose(agent)
            _forget_quietly(self._threads_dir, thread_id)
            raise
        return self._begin(incoming, thread_id, agent)

    async def _agent_for(self, thread_id: str, cwd: str) -> Any:
        del thread_id
        async with self._create_lock:
            config = prepare_agent_config(
                self._config,
                cwd=cwd,
                instructions_path=completions_special_instructions_path(),
            )
            return await self._agent_factory(
                config,
                auth_connector=self._auth,
                tools_cache=self._tools_cache,
                os_backend=self._os_backend,
            )

    def _begin(
        self, incoming: IncomingTurn, thread_id: str, agent: Any
    ) -> TurnRun:
        run = TurnRun(
            agent=agent,
            content=ContentMap(),
            flow=ToolFlow(thread_data_path(self._threads_dir, thread_id)),
            prompt=incoming.prompt,
            thread_id=thread_id,
            threads_dir=self._threads_dir,
            completion_id="chatcmpl_" + str(uuid.uuid4()),
            created=int(time.time()),
            model=incoming.model,
        )
        run.start()
        return run


def _forget_quietly(threads_dir: str, thread_id: str) -> None:
    try:
        forget_thread(threads_dir, thread_id)
    except Exception:
        logger.exception("completions delete thread")


def _threads_dir(config: dict) -> str:
    return resolve_threads_dir(config)


def _cors_origins(config: dict) -> list[str]:
    raw = config.get("chat_api_cors_origins") or []
    if not isinstance(raw, list):
        return []
    return [
        item for item in raw if isinstance(item, str) and item not in {"", "*"}
    ]
