"""Один ход диалога: поток агента, вопрос человеку, терминальное состояние."""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Callable

from gc_core.execution.human import HumanRequestInterrupted
from gc_core.tools.turn.submit_solution import SubmitSolution

from gc_a2a.store import TaskStore

logger = logging.getLogger(__name__)

SYSTEM = "a2a"


def answer_text(agent: Any) -> str:
    raw = SubmitSolution.present_result(getattr(agent, "last_answer", None))
    if raw is None:
        return ""
    if isinstance(raw, str):
        return raw
    return str(raw)


# Пиру в status.message — без путей и текста исключения ядра.
PEER_FAILURE_TEXT = "Ход не выполнен."


def error_text(exc: BaseException) -> str:
    del exc
    return PEER_FAILURE_TEXT


class Turn:
    def __init__(
        self,
        *,
        agent: Any,
        store: TaskStore,
        task_id: str,
        context_id: str,
        prompt: str,
        new_id: Callable[[], str],
        release: Callable[[str, str], None],
    ) -> None:
        self.agent = agent
        self._store = store
        self.task_id = task_id
        self._context_id = context_id
        self._prompt = prompt
        self._new_id = new_id
        self._release = release
        self._wait: asyncio.Future | None = None
        self.cancel_requested = False
        self.sealed = False
        self.done = asyncio.Event()
        self._task: asyncio.Task | None = None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def ask(self, question: str) -> str:
        if self.cancel_requested or self.sealed:
            raise HumanRequestInterrupted()
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        self._wait = future
        await self._store.mark_input_required(
            self.task_id, self._new_id(), question
        )
        if self.cancel_requested or self.sealed:
            raise HumanRequestInterrupted()
        try:
            return await future
        except asyncio.CancelledError as exc:
            raise HumanRequestInterrupted() from exc

    def answer(self, text: str) -> None:
        wait = self._wait
        if wait is not None and not wait.done():
            wait.set_result(text)

    async def cancel(self) -> None:
        self.cancel_requested = True
        wait = self._wait
        if wait is not None and not wait.done():
            wait.cancel()
        stop = getattr(self.agent, "stop", None)
        if stop is not None:
            stop()

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
        await self._seal(failed)

    async def _forward(self) -> None:
        if self.cancel_requested:
            return
        stream = self.agent.stream(
            self._prompt, thread_id=self._context_id, system=SYSTEM
        )
        try:
            async for _kind, _text in stream:
                if self.cancel_requested:
                    break
        finally:
            # ``return``/``break`` из ``async for`` не ждут ``finally`` генератора:
            # прогон ``AsyncAgent.stream`` остаётся жить, а диалог уже свободен.
            close = getattr(stream, "aclose", None)
            if close is not None:
                await close()

    async def _seal(self, failed: BaseException | None) -> None:
        if self.sealed:
            self.done.set()
            return
        self.sealed = True
        try:
            await self._record(failed)
        except Exception:
            logger.exception("a2a seal")
            await self._record_failure(failed)
        self._release(self._context_id, self.task_id)
        self.done.set()

    async def _record(self, failed: BaseException | None) -> None:
        if self.cancel_requested:
            await self._store.mark_canceled(self.task_id)
            return
        if failed is not None:
            await self._store.mark_failed(
                self.task_id, self._new_id(), error_text(failed)
            )
            return
        await self._store.mark_completed(
            self.task_id, answer_text(self.agent), self._new_id()
        )

    async def _record_failure(self, failed: BaseException | None) -> None:
        try:
            await self._store.mark_failed(
                self.task_id,
                self._new_id(),
                error_text(failed or RuntimeError("seal")),
            )
        except Exception:
            logger.exception("a2a seal failed")
            await self._store.mark_broken(self.task_id)
