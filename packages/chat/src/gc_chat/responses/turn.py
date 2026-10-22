"""Один ход: ``AsyncAgent.stream``, карточки из jsonl, терминальный статус."""

from __future__ import annotations

import asyncio
import logging
from typing import Any

from gc_core.tools.turn.turn_close import (
    close_view,
    closing_reply_of,
    without_repeated_body,
)

from gc_chat.responses.index_store import ResponseIndex, ResponseRecord
from gc_chat.responses.sse import encode_sse, is_terminal
from gc_chat.responses.status import incomplete_details, turn_status
from gc_chat.responses.stream_map import StreamMap
from gc_chat.responses.tool_flow import ToolFlow

logger = logging.getLogger(__name__)

_TOOL_POLL_S = 0.05
SYSTEM = "responses"


def outcome_of(agent: Any) -> str | None:
    executor = getattr(agent, "executor", None)
    if executor is None:
        return None
    outcome = getattr(executor, "last_terminal_outcome", None)
    if outcome is None or isinstance(outcome, str):
        return outcome
    return None


def error_message(exc: BaseException) -> str:
    text = str(exc).strip()
    if text:
        return text
    return exc.__class__.__name__


class TurnRun:
    """Ход, который живёт отдельно от HTTP-запроса."""

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

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def wait_client(self, request: Any) -> dict:
        while not self.done.is_set():
            if await _disconnected(request):
                await self.request_stop()
            try:
                await asyncio.wait_for(self.done.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue
        return self.result()

    def result(self) -> dict:
        loaded = self._index.load(self.response_id)
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
        _stop_agent(self.agent)

    def _stop_if_open(self) -> None:
        if self.sealed or self.cancel_requested:
            return
        asyncio.get_running_loop().create_task(self.request_stop())

    async def _run(self) -> None:
        failed: BaseException | None = None
        stop = asyncio.Event()
        watch = asyncio.create_task(self._watch(stop))
        try:
            async with self._emit_lock:
                _put(self._queue, self._mapper.opening_events())
            await self._forward()
        except asyncio.CancelledError:
            self.cancel_requested = True
        except Exception as exc:
            failed = exc
            logger.exception("responses turn failed")
        stop.set()
        late_cancel = False
        try:
            await _join(watch)
            await self._after_stream(failed)
        except asyncio.CancelledError:
            self.cancel_requested = True
            late_cancel = True
        except Exception as exc:
            if failed is None:
                failed = exc
                logger.exception("responses turn failed")
        await self._seal(failed)
        if late_cancel:
            raise asyncio.CancelledError

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
                _put(self._queue, self._tool_events())
            try:
                await asyncio.wait_for(stop.wait(), _TOOL_POLL_S)
            except TimeoutError:
                continue

    async def _after_stream(self, failed: BaseException | None) -> None:
        async with self._emit_lock:
            events = self._tool_events()
            if failed is None and not self.cancel_requested:
                events.extend(self._closing_events())
            _put(self._queue, events)

    def _closing_events(self) -> list[dict]:
        answer = getattr(self.agent, "last_answer", None)
        close = close_view(answer, closing_reply_of(self.agent))
        if close is None:
            return self._mapper.flush_unseen(answer)
        shown = bool(self._mapper.shown_answer)
        close = without_repeated_body(close, self._mapper.last_message_text)
        return self._mapper.append_answer(
            close.render(markdown=True, after_shown_reply=shown)
        )

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
            self._index.save(self._record)
            self.sealed = True
            self._release_busy(self.thread_id, self.response_id)
            self.done.set()

    def _tool_events(self) -> list[dict]:
        events: list[dict] = []
        for report in self._flow.poll():
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


async def _join(task: asyncio.Task) -> None:
    try:
        await task
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("responses tool watch")


def _stop_agent(agent: Any) -> None:
    stop = getattr(agent, "stop", None)
    if stop is not None:
        stop()


async def _disconnected(request: Any) -> bool:
    probe = getattr(request, "is_disconnected", None)
    if probe is None:
        return False
    try:
        return bool(await probe())
    except Exception:
        return False
