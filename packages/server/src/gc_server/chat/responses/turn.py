"""Один ход: ``AsyncAgent.stream``, карточки из хвоста треда, терминальный статус."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import replace
from typing import Any

from gc_core.tools.turn.turn_close import (
    close_view,
    closing_reply_of,
    without_repeated_body,
)

from gc_server.chat.http_common import disconnected
from gc_server.chat.model_errors import model_api_error
from gc_server.chat.responses.index_store import (
    ResponseIndex,
    ResponseRecord,
    thread_key,
)
from gc_server.chat.responses.sse import encode_sse, is_terminal
from gc_server.chat.responses.status import incomplete_details, turn_status
from gc_server.chat.responses.stream_map import StreamMap
from gc_server.chat.tool_flow import ToolFlow
from gc_server.chat.turn_common import (
    TurnLifecycle,
    astop_agent,
    log_forgotten_stop,
    outcome_of,
)

logger = logging.getLogger(__name__)

_TOOL_POLL_S = 0.05
_FINALIZATION_BATCHES = 3
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
        self.persistence_failed = False
        self._finalization_batches = 0
        self._failed_response: dict | None = None
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
        if self.persistence_failed:
            return self._failed_response
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
        await astop_agent(self.agent)

    def _stop_if_open(self) -> None:
        if self.sealed or self.cancel_requested:
            return
        task = asyncio.get_running_loop().create_task(self.request_stop())
        task.add_done_callback(log_forgotten_stop)

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
            self._prompt,
            thread_id=self.thread_id,
            system=SYSTEM,
            # Отмена этого ответа с другого инстанса называет его.
            cancel_target=self.response_id,
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
            body = events[-1]["response"]
            self._record = replace(self._record, status=status, response=body)
            self._finalization_batches += 1
            try:
                try:
                    saved = await self._save_final()
                except asyncio.CancelledError:
                    # Отмена записи не отменяет уже выполненные действия.
                    # Клиент всё равно получает закрывающие события и ошибку.
                    self._report_persistence_failure(events)
                    raise
                if saved:
                    _put(self._queue, events)
                else:
                    self._report_persistence_failure(events)
            finally:
                self.sealed = True
                try:
                    if not self.persistence_failed:
                        await self._release_busy(
                            self.thread_id, self.response_id
                        )
                finally:
                    self.done.set()

    def _report_persistence_failure(self, events: list[dict]) -> None:
        self.persistence_failed = True
        self._failed_response = {
            **self._record.response,
            "status": "failed",
            "error": {
                "code": "response_persistence_failed",
                "message": "Response could not be saved. Do not repeat the request; check its status later.",
            },
            "incomplete_details": None,
        }
        _put(self._queue, events[:-1])
        _put(
            self._queue,
            [{"type": "response.failed", "response": self._failed_response}],
        )

    async def _save_final(self) -> bool:
        for attempt in range(3):
            try:
                await self._index.save(self._record)
                return True
            except Exception:
                if attempt == 2:
                    logger.exception(
                        "responses final status could not be saved"
                    )
                    return False
                await asyncio.sleep(0.05)
        return False

    async def retry_finalization(self) -> None:
        """Повторяется только запись итога, пока бронь удерживает этот процесс.

        Агент повторно не запускается; клиент не получает 200 in_progress
        после ошибки записи. Полная недоступность хранилища вместе с гибелью
        процесса не позволяет гарантировать сохранность ещё не записанного итога.
        """
        async with self._seal_lock:
            if not self.persistence_failed:
                return
            self._finalization_batches += 1
            try:
                await self._retry_owned_result()
            finally:
                if self._finalization_batches >= _FINALIZATION_BATCHES:
                    # execution_started запрещает повтор действий. После
                    # восстановления хранилища recovery закроет неизвестный итог.
                    await self._release_busy(self.thread_id, self.response_id)

    async def _retry_owned_result(self) -> None:
        thread = await self._index.threads.get(thread_key(self.thread_id))
        frontend = thread.frontend if thread else {}
        admission = frontend.get("chat", {}).get("admission") or {}
        if admission.get("token") != self._record.reservation:
            logger.warning(
                "responses finalization lost admission: %s", self.response_id
            )
            await self._release_busy(self.thread_id, self.response_id)
        elif await self._save_final():
            self.persistence_failed = False
            await self._release_busy(self.thread_id, self.response_id)

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
        exc = model_api_error(failed)
        error = {"message": exc.message, "code": exc.code}
    return status, error, incomplete_details(status, outcome)


def _put(queue: asyncio.Queue, events: list[dict]) -> None:
    for event in events:
        queue.put_nowait(event)
