"""Одна asyncio-задача открывает, вызывает и закрывает одну SDK-сессию."""

from __future__ import annotations

import asyncio
from contextvars import Context, copy_context

from gc_core.mcp_session_owner import use_owner


class SessionTask:
    """Команды выполняются inline: anyio-контексты не переходят между задачами."""

    def __init__(self, owner, close):
        self._queue = asyncio.Queue()
        self._close = close
        self._pending = set()
        self._accepting = True
        self._active = None
        self.task = asyncio.create_task(
            self._run(owner), name="mcp-session", context=Context()
        )

    async def call(self, callback):
        if not self._accepting or self.task.done():
            raise RuntimeError("MCP session owner is closed")
        future = asyncio.get_running_loop().create_future()
        self._pending.add(future)
        self._queue.put_nowait((callback, copy_context(), future))
        try:
            return await asyncio.shield(future)
        except asyncio.CancelledError:
            future.cancel()
            # Настоящая отмена вызова закрывает его ресурс, а не бросает
            # выполнение в фоне. Остальные серверы имеют других владельцев.
            if self._active is future:
                self._accepting = False
                self.task.cancel()
                await asyncio.shield(self.task)
            raise

    async def close(self):
        if self._accepting:
            self._accepting = False
            self._queue.put_nowait(None)
        await asyncio.shield(self.task)

    async def _invoke(self, command):
        callback, context, future = command
        if future.cancelled():
            self._pending.discard(future)
            return
        self._active = future
        tokens = [(var, var.set(value)) for var, value in context.items()]
        try:
            result = await callback()
            if not future.done():
                future.set_result(result)
        except Exception as exc:
            if not future.done():
                future.set_exception(exc)
        finally:
            for var, token in reversed(tokens):
                var.reset(token)
            self._active = None
            if future.done():
                self._pending.discard(future)

    async def _run(self, owner):
        with use_owner(owner):
            try:
                while (command := await self._queue.get()) is not None:
                    await self._invoke(command)
            except asyncio.CancelledError:
                # Отмена transport task group не должна отменять задачу агента.
                pass
            finally:
                self._accepting = False
                try:
                    await self._close()
                finally:
                    for future in self._pending:
                        if not future.done():
                            future.set_exception(
                                RuntimeError(
                                    "MCP session owner stopped during call"
                                )
                            )
                    self._pending.clear()
