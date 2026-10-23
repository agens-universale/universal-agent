"""Один ход: ``AsyncAgent.stream``, карточки из хвоста треда, затем забвение треда."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gc_core.threads.facade import Threads
from gc_core.tools.turn.turn_close import close_view, closing_reply_of

from gc_server.chat.completions.content import ContentMap
from gc_server.chat.completions.history import SYSTEM, forget_thread
from gc_server.chat.completions.status import finish_reason
from gc_server.chat.errors import ApiError, error_payload
from gc_server.chat.tool_flow import ToolFlow
from gc_server.chat.turn_common import (
    TurnLifecycle,
    astop_agent,
    error_message,
    log_forgotten_stop,
    outcome_of,
)

logger = logging.getLogger(__name__)

_TOOL_POLL_S = 0.05


class TurnRun(TurnLifecycle):
    """Ход одного запроса. После терминального статуса тред удаляется."""

    label = "completions"

    def __init__(
        self,
        *,
        agent: Any,
        content: ContentMap,
        flow: ToolFlow,
        prompt: str,
        thread_id: str,
        threads: Threads,
        completion_id: str,
        created: int,
        model: str,
    ) -> None:
        self.agent = agent
        self._content = content
        self._flow = flow
        self._prompt = prompt
        self.thread_id = thread_id
        self._threads = threads
        self.completion_id = completion_id
        self.created = created
        self.model = model
        self._queue: asyncio.Queue = asyncio.Queue()
        self._emit_lock = asyncio.Lock()
        self._seal_lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.sealed = False
        self.cancel_requested = False
        self._task: asyncio.Task | None = None

    async def pull(self, timeout: float) -> tuple | None:
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except TimeoutError:
            return None

    async def request_stop(self) -> None:
        async with self._seal_lock:
            if self.sealed:
                return
            self.cancel_requested = True
        await astop_agent(self.agent)

    def stop_if_open(self) -> None:
        if self.sealed or self.cancel_requested:
            return
        task = asyncio.get_running_loop().create_task(self.request_stop())
        task.add_done_callback(log_forgotten_stop)

    async def _forward(self) -> None:
        if self.cancel_requested:
            return
        async for kind, text in self.agent.stream(
            self._prompt, thread_id=self.thread_id, system=SYSTEM
        ):
            async with self._emit_lock:
                self._put(self._content.feed(kind, text))

    async def _watch(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            async with self._emit_lock:
                await self._put_tools()
            try:
                await asyncio.wait_for(stop.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue

    async def _after_stream(self, failed: BaseException | None) -> None:
        async with self._emit_lock:
            await self._put_tools()
            if failed is None and not self.cancel_requested:
                answer = getattr(self.agent, "last_answer", None)
                close = close_view(answer, closing_reply_of(self.agent))
                if close is not None:
                    answer = close.render(markdown=True)
                self._put(self._content.finish(answer))

    async def _seal(self, failed: BaseException | None) -> None:
        async with self._seal_lock:
            if self.sealed:
                self.done.set()
                return
            item = _terminal(self.cancel_requested, failed, self.agent)
            self.sealed = True
        await self._forget()
        self._queue.put_nowait(item)
        self.done.set()

    async def _forget(self) -> None:
        try:
            await forget_thread(self._threads, self.thread_id)
        except Exception:
            logger.exception("completions delete thread")
        await _safe_aclose(self.agent)

    async def _put_tools(self) -> None:
        chunks: list[str] = []
        for report in await self._flow.poll():
            chunks.extend(self._content.apply_tool(report))
        self._put(chunks)

    def _put(self, chunks: list[str]) -> None:
        for chunk in chunks:
            self._queue.put_nowait(("content", chunk))


def _terminal(
    cancelled: bool, failed: BaseException | None, agent: Any
) -> tuple:
    if cancelled:
        return ("cancelled", None)
    if failed is not None:
        exc = ApiError(500, "server_error", error_message(failed))
        return ("error", error_payload(exc))
    return ("finish", finish_reason(outcome_of(agent)))


async def _safe_aclose(agent: Any) -> None:
    close = getattr(agent, "aclose", None)
    if close is None:
        return
    try:
        await close()
    except Exception:
        logger.exception("completions aclose")
