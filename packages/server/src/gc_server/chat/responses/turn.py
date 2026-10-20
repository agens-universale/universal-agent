"""Один ход: ``AsyncAgent.stream``, карточки из хвоста треда, терминальный статус."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gc_core.tools.turn.submit_solution import SubmitSolution

from gc_server.chat.http_common import disconnected
from gc_server.chat.responses.index_store import ResponseIndex, ResponseRecord
from gc_server.chat.responses.sse import encode_sse, is_terminal
from gc_server.chat.responses.status import incomplete_details, turn_status
from gc_server.chat.responses.stream_map import StreamMap
from gc_server.chat.tool_flow import ToolFlow
from gc_server.chat.turn_common import (
    TurnLifecycle,
    error_message,
    outcome_of,
    stop_agent,
)

logger = logging.getLogger(__name__)

_TOOL_POLL_S = 0.05
SYSTEM = "responses"


class TurnRun(TurnLifecycle):
    """Ход, который живёт отдельно от HTTP-запроса."""

    label = "responses"

    def __init__(
        self,
        *,
        agent: Any,
        mapper: StreamMap,
        flow: ToolFlow,
        index: ResponseIndex,
        record: ResponseRecord,
        prompt: str,
        thread_id: str,
        release_busy: Any,
    ) -> None:
        self.agent = agent
        self._mapper = mapper
        self._flow = flow
        self._index = index
        self._record = record
        self._prompt = prompt
        self.thread_id = thread_id
        self._release_busy = release_busy
        self._queue: asyncio.Queue = asyncio.Queue()
        self._emit_lock = asyncio.Lock()
        self._seal_lock = asyncio.Lock()
        self.done = asyncio.Event()
        self.sealed = False
        self.cancel_requested = False
        self.response_id = record.response_id
        self._task: asyncio.Task | None = None

    async def wait_client(self, request: Any) -> dict:
        while not self.done.is_set():
            if await disconnected(request):
                await self.request_stop()
            try:
                await asyncio.wait_for(self.done.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue
        return await self.result()

    async def result(self) -> dict:
        loaded = await self._index.load(self.response_id)
        if loaded is None:
            return self._record.response
        return loaded.response

    async def iter_sse(self):
        try:
            while True:
                event = await self._queue.get()
                yield encode_sse(event)
                if is_terminal(event):
                    return
        finally:
            self._stop_if_open()

    async def request_stop(self) -> None:
        async with self._seal_lock:
            if self.sealed:
                return
            self.cancel_requested = True
        async with self._seal_lock:
            if self.sealed:
                return
        stop_agent(self.agent)

    def _stop_if_open(self) -> None:
        if self.sealed or self.cancel_requested:
            return
        asyncio.get_running_loop().create_task(self.request_stop())

    async def _open(self) -> None:
        async with self._emit_lock:
            _put(self._queue, self._mapper.opening_events())

    async def _forward(self) -> None:
        # Отмена до stream() пишет stop в state прошлого прогона, а
        # AsyncAgent.run его сбрасывает: модель отработала бы целиком,
        # клиенту всё равно ушёл бы cancelled.
        if self.cancel_requested:
            return
        async for kind, text in self.agent.stream(
            self._prompt, thread_id=self.thread_id, system=SYSTEM
        ):
            async with self._emit_lock:
                _put(self._queue, self._mapper.feed(kind, text))

    async def _watch(self, stop: asyncio.Event) -> None:
        while not stop.is_set():
            async with self._emit_lock:
                _put(self._queue, await self._tool_events())
            try:
                await asyncio.wait_for(stop.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue

    async def _after_stream(self, failed: BaseException | None) -> None:
        async with self._emit_lock:
            events = await self._tool_events()
            if failed is None and not self.cancel_requested:
                answer = SubmitSolution.present_result(
                    getattr(self.agent, "last_answer", None),
                    shown=self._mapper.shown_answer,
                )
                events.extend(self._mapper.flush_unseen(answer))
            _put(self._queue, events)

    async def _seal(self, failed: BaseException | None) -> None:
        async with self._seal_lock:
            if self.sealed:
                self.done.set()
                return
            status, error, details = _decision(
                self.cancel_requested, failed, outcome_of(self.agent)
            )
            events = self._mapper.seal_events(
                status, error=error, incomplete_details=details
            )
            _put(self._queue, events)
            body = events[-1]["response"]
            self._record = ResponseRecord(
                self.response_id,
                self._record.thread_id,
                self._record.cwd,
                self._record.previous_response_id,
                self._record.created_at,
                status,
                body,
            )
            await self._index.save(self._record)
            self.sealed = True
            self._release_busy(self.thread_id, self.response_id)
            self.done.set()

    async def _tool_events(self) -> list[dict]:
        events: list[dict] = []
        for report in await self._flow.poll():
            events.extend(self._mapper.apply_tool(report))
        return events


def _decision(
    cancelled: bool, failed: BaseException | None, outcome: str | None
) -> tuple[str, dict | None, dict | None]:
    status = turn_status(
        outcome=outcome, cancelled=cancelled, failed=failed is not None
    )
    error = None
    if status == "failed" and failed is not None:
        error = {"message": error_message(failed), "code": "server_error"}
    return status, error, incomplete_details(status, outcome)


def _put(queue: asyncio.Queue, events: list[dict]) -> None:
    for event in events:
        queue.put_nowait(event)
