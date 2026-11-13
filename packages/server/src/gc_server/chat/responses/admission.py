"""Межпроцессная бронь диалога до создания агента и до финальной сводки."""

from __future__ import annotations

import asyncio
import logging
import time
import uuid

from gc_core.threads.errors import ThreadConflict
from gc_core.threads.models import SideChange

from gc_server.chat.errors import ApiError

logger = logging.getLogger(__name__)
SECTION = "chat"
FIELD = "admission"
TTL = 120.0
RENEW = 20.0


class Admission:
    def __init__(self, threads, key, on_lost):
        self.threads, self.key, self.on_lost = threads, key, on_lost
        self.token = uuid.uuid4().hex
        self.keeper = None
        self.lost = False
        self._deadline = 0.0

    async def acquire(self):
        for _ in range(16):
            record = await self.threads.get(self.key)
            frontend = record.frontend if record else {}
            current = frontend.get(SECTION, {}).get(FIELD)
            if current and current.get("expires", 0) > time.time():
                raise ApiError(
                    409, "conflict", "turn already running on another instance"
                )
            try:
                deadline = time.monotonic() + TTL
                await self._write(
                    frontend,
                    {"token": self.token, "expires": time.time() + TTL},
                )
                self._deadline = deadline
                self.keeper = asyncio.create_task(self._keep())
                return
            except ThreadConflict:
                continue
        raise ApiError(409, "conflict", "dialog changed; retry the request")

    async def _write(self, frontend, value):
        await self.threads.store.append_side(
            self.key,
            SideChange(
                op_id=f"chat-admission-{uuid.uuid4().hex}",
                expected_frontend=frontend,
                patch_frontend=(SECTION, {FIELD: value}),
            ),
        )

    async def _refresh(self, *, release=False):
        for _ in range(16):
            record = await self.threads.get(self.key)
            frontend = record.frontend if record else {}
            current = frontend.get(SECTION, {}).get(FIELD) or {}
            if current.get("token") != self.token:
                return False
            if not release and current.get("expires", 0) <= time.time():
                return False
            try:
                deadline = time.monotonic() + TTL
                await self._write(
                    frontend,
                    None
                    if release
                    else {
                        "token": self.token,
                        "expires": time.time() + TTL,
                    },
                )
                if not release:
                    self._deadline = deadline
                return True
            except ThreadConflict:
                continue
        raise ThreadConflict("не удалось продлить бронь chat")

    async def _keep(self):
        while time.monotonic() < self._deadline:
            await asyncio.sleep(
                min(RENEW, max(0, self._deadline - time.monotonic()))
            )
            try:
                if not await self._refresh():
                    break
            except Exception:
                # Временный сбой не прекращает продление навсегда. После
                # истечения подтверждённой брони владение не предполагаем.
                logger.warning(
                    "responses admission renewal failed", exc_info=True
                )
        self.lost = True
        await self.on_lost()

    async def validate(self):
        if self.lost or not await self._refresh():
            raise ThreadConflict("бронь диалога потеряна до запуска")

    async def release(self):
        if (
            self.keeper is not None
            and self.keeper is not asyncio.current_task()
        ):
            self.keeper.cancel()
            await asyncio.gather(self.keeper, return_exceptions=True)
        await self._refresh(release=True)
