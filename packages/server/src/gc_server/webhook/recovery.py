"""Восстановление задач вебхука после падения инстанса (спека сервера, 13.4).

Бесхозный тред — без живой аренды и либо с ходом, отмеченным идущим
(``runtime.turn == "running"``: процесс умер посреди хода), либо с
недоставленным во входящем потоке. Каждый инстанс при старте и раз в
``webhook.recovery.interval_seconds`` обходит треды, обновлённые за
``lookback_hours``, и продолжает такие ходом: прерванному — с техническим
сообщением о сбое. Захват треда выигрывает один инстанс; остальные отдают
ему задачу. Последний вызов тула прерванного хода может повториться
(at-least-once).

Треды других поверхностей и интерактивных сессий (резерв ``system``) и
задачи swarm (их песочницы ведёт монитор swarm) не трогаются.
"""

from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey, ThreadRecord
from gc_core.threads.rules import lease_expired

from gc_server.config import RESERVED_SYSTEMS
from gc_server.webhook import status as task_status

if TYPE_CHECKING:
    from gc_server.webhook.surface import WebhookSurface

logger = logging.getLogger(__name__)

RECOVERY_MESSAGE = (
    "Предыдущий ход прерван сбоем сервера. Проверь, что уже сделано, и "
    "продолжи задачу."
)
#: Состояние фронтенда swarm: задачу ведёт песочница, а не процесс сервера.
_SWARM = "swarm"


async def arecover_once(
    surface: WebhookSurface, *, now: datetime
) -> list[ThreadKey]:
    """Один обход: треды, по которым этот инстанс запустил продолжение."""
    lookback = timedelta(hours=surface.config.recovery.lookback_hours)
    threads = surface.threads()
    taken: list[ThreadKey] = []
    async for record in threads.iter_threads(
        updated_after=(now - lookback).isoformat()
    ):
        if not _orphaned(record, now):
            continue
        if await _acontinue(surface, threads, record):
            taken.append(record.key)
    return taken


async def arecover_forever(surface: WebhookSurface) -> None:
    """Обход при старте и затем раз в ``interval_seconds``."""
    from gc_core.execution.environment import utc_now

    interval = surface.config.recovery.interval_seconds
    while True:
        try:
            taken = await arecover_once(surface, now=utc_now())
            if taken:
                logger.info(
                    "recovery: продолжены задачи %s",
                    ", ".join(f"{k.system}/{k.thread}" for k in taken),
                )
        except Exception:
            logger.warning("recovery pass failed", exc_info=True)
        await asyncio.sleep(interval)


def _orphaned(record: ThreadRecord, now: datetime) -> bool:
    if record.key.system in RESERVED_SYSTEMS or _SWARM in record.frontend:
        return False
    if record.lease is not None and not lease_expired(record.lease, now):
        return False
    pending = record.max_seq.get("inbox", 0) > record.state.inbox_seq
    return record.runtime.turn == "running" or pending


async def _acontinue(
    surface: WebhookSurface, threads: Threads, record: ThreadRecord
) -> bool:
    key = record.key
    if record.runtime.turn == "running":
        await _anote_interruption(threads, record)
    return await surface.astart_recovered(key.system, key.thread)


async def _anote_interruption(threads: Threads, record: ThreadRecord) -> None:
    """Техническое сообщение о сбое — во входящий поток, один раз на
    прерванный ход: инстансы, увидевшие тот же тред, пишут одну и ту же
    операцию (``op_id`` от поколения и номера последней записи истории)."""
    key = record.key
    mark = f"{record.generation}_{record.max_seq.get('messages', 0)}"
    await threads.submit(
        key,
        {
            "id": f"m_recovery_{mark}",
            "role": "user",
            "content": wrap_technical_user_content(RECOVERY_MESSAGE),
        },
        op_id=f"recovery:{key.system}:{key.thread}:{mark}",
        source="recovery",
    )
    status = task_status.status_of(record)
    if status is not None and status.get("status") == "running":
        await task_status.aset_status(threads, key, status="interrupted")
