"""Общее у хода Responses и Chat Completions: исход, текст сбоя, жизненный цикл.

Ход живёт в своей задаче: поток дельт агента, рядом — опрос карточек тулов;
после потока — хвост и запечатывание. Отмена во время потока и во время хвоста
различаются: поздняя отмена запечатывает ход и пробрасывается дальше.
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

logger = logging.getLogger(__name__)


def outcome_of(agent: Any) -> str | None:
    """Терминальный исход последнего хода исполнителя агента или ``None``."""
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


def stop_agent(agent: Any) -> None:
    stop = getattr(agent, "stop", None)
    if stop is not None:
        stop()


class TurnLifecycle:
    """Каркас хода. Подкласс задаёт ``label`` (для лога) и шаги:
    ``_open`` (до потока), ``_forward`` (поток), ``_watch`` (опрос карточек),
    ``_after_stream`` и ``_seal``."""

    label = "chat"
    cancel_requested: bool
    _task: asyncio.Task | None

    def start(self) -> None:
        self._task = asyncio.create_task(self._run())

    async def _open(self) -> None:
        return None

    async def _forward(self) -> None:
        raise NotImplementedError

    async def _watch(self, stop: asyncio.Event) -> None:
        raise NotImplementedError

    async def _after_stream(self, failed: BaseException | None) -> None:
        raise NotImplementedError

    async def _seal(self, failed: BaseException | None) -> None:
        raise NotImplementedError

    async def _run(self) -> None:
        failed: BaseException | None = None
        stop = asyncio.Event()
        watch = asyncio.create_task(self._watch(stop))
        try:
            await self._open()
            await self._forward()
        except asyncio.CancelledError:
            self.cancel_requested = True
        except Exception as exc:
            failed = exc
            logger.exception("%s turn failed", self.label)
        stop.set()
        late_cancel = False
        try:
            await self._join(watch)
            await self._after_stream(failed)
        except asyncio.CancelledError:
            self.cancel_requested = True
            late_cancel = True
        except Exception as exc:
            if failed is None:
                failed = exc
                logger.exception("%s turn failed", self.label)
        await self._seal(failed)
        if late_cancel:
            raise asyncio.CancelledError

    async def _join(self, task: asyncio.Task) -> None:
        try:
            await task
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("%s tool watch", self.label)
