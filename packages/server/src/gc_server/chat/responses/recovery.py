"""Продолжение осиротевших Responses под новой межпроцессной бронью."""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import replace

from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)

from gc_server.chat.errors import ApiError
from gc_server.chat.model_errors import model_api_error
from gc_server.chat.responses.admission import Admission
from gc_server.chat.responses.index_store import SYSTEM, thread_key

logger = logging.getLogger(__name__)
_RUNNING = "in_progress"


class ResponsesRecovery:
    def __init__(self, facade, config):
        self.facade = facade
        self.config = config
        self._task = None

    def start(self):
        if self._task is None:
            self._task = asyncio.create_task(self._loop())

    async def stop(self):
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def _loop(self):
        while True:
            try:
                await self.recover_once()
            except Exception:
                logger.exception("responses recovery")
            await asyncio.sleep(self.config.interval_seconds)

    async def recover_once(self):
        for run in list(self.facade._live.values()):
            if run.persistence_failed:
                await run.retry_finalization()
        await self._recover_records()

    async def _recover_records(self):
        index = self.facade._index
        # Старый файловый архив читается один раз и вне HTTP event loop.
        legacy = await asyncio.to_thread(index.legacy_dialogs)
        ids = {
            tid
            for tid, records in legacy.items()
            if any(record.status == _RUNNING for record in records)
        }
        async for thread in index.threads.iter_threads(system=SYSTEM):
            summary = index.summary(thread)
            if summary and summary.get("status") == _RUNNING:
                ids.add(thread.key.thread)
        for thread_id in ids:
            try:
                for record in await index.thread_records(
                    thread_id, legacy_records=legacy.get(thread_id, [])
                ):
                    await self.recover(record)
            except Exception:
                logger.exception("responses recovery thread %s", thread_id)

    async def recover(self, record):
        facade = self.facade
        if record.status != _RUNNING or record.thread_id in facade._busy:
            return
        facade._busy.add(record.thread_id)
        started = False
        acquired = False
        try:
            await facade._refuse_foreign_turn(record.thread_id)
            admission = Admission(
                facade._index.threads,
                thread_key(record.thread_id),
                lambda: facade._lost_admission(record.thread_id),
            )
            await admission.acquire()
            acquired = True
            facade._admissions[record.thread_id] = admission
            latest = await facade._index.load(record.response_id)
            if latest is not None and latest.status == _RUNNING:
                owned = replace(latest, reservation=admission.token)
                started = await self._resume(owned, admission)
        except ApiError as exc:
            if exc.status != 409:
                raise
        finally:
            if not started:
                if acquired:
                    await facade._release(record.thread_id, "")
                else:
                    facade._busy.discard(record.thread_id)

    async def _reason(self, record):
        threads = self.facade._index.threads
        key = thread_key(record.thread_id)
        for entry in await threads.read_frontend(key, "control"):
            if (
                entry.data.get("type") == "cancel"
                and entry.data.get("target") == record.response_id
            ):
                return "cancelled"
        thread = await threads.get(key)
        summary = self.facade._index.summary(thread) if thread else None
        if summary and summary.get("latest_response_id") != record.response_id:
            return "execution_interrupted"
        if not self.config.enabled or not record.request_text:
            return "execution_interrupted"
        if time.time() - record.created_at > self.config.lookback_hours * 3600:
            return "execution_interrupted"
        if record.recovery_attempts >= self.config.max_attempts:
            return "recovery_exhausted"
        return None

    async def _resume(self, record, admission):
        reason = await self._reason(record)
        if reason is not None:
            await self._finish(record, reason)
            return False
        facade = self.facade
        record = replace(
            record, recovery_attempts=record.recovery_attempts + 1
        )
        await facade._index.save(record)
        try:
            cwd = facade.new_dialog_cwd(record.cwd)
            agent = await facade._agent_for(record.thread_id, cwd)
            await admission.validate()
            prompt = (
                wrap_technical_user_content(
                    "Предыдущий ход прерван сбоем сервера. Проверь историю и уже "
                    "выполненные действия, затем продолжи исходную задачу. "
                    "Не повторяй завершённые действия без необходимости.\n"
                )
                + "\nИсходный запрос пользователя:\n"
                + record.request_text
            )
            await facade._launch(record, agent, prompt)
            return True
        except Exception as exc:
            logger.exception("responses recovery launch")
            error = model_api_error(exc)
            permanent = isinstance(exc, ApiError) and 400 <= exc.status < 500
            if (
                permanent
                or record.recovery_attempts >= self.config.max_attempts
            ):
                await self._finish(record, error.code, error.message)
            return False

    async def _finish(
        self, record, code, message="Response interrupted by server restart."
    ):
        status = "cancelled" if code == "cancelled" else "failed"
        body = {
            **record.response,
            "status": status,
            "error": None
            if status == "cancelled"
            else {"code": code, "message": message},
        }
        thread = await self.facade._index.threads.get(
            thread_key(record.thread_id)
        )
        summary = self.facade._index.summary(thread) if thread else None
        update_summary = (
            not summary
            or summary.get("latest_response_id") == record.response_id
        )
        await self.facade._index.save(
            replace(record, status=status, response=body),
            update_summary=update_summary,
        )
