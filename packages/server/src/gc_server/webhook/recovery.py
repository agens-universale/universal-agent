"""Восстановление задач вебхука после падения инстанса (спека сервера, 13.4).

Бесхозный тред — без живой аренды и либо с ходом, отмеченным идущим
(``runtime.turn == "running"``: процесс умер посреди хода), либо с
недоставленным во входящем потоке. Каждый инстанс при старте и раз в
``webhook.recovery.interval_seconds`` обходит треды, обновлённые за
``lookback_hours``, и продолжает такие ходом: прерванному — с техническим
сообщением о сбое. Захват треда выигрывает один инстанс; остальные отдают
ему задачу. Последний вызов тула прерванного хода может повториться
(at-least-once).

Берутся только задачи вебхука — треды с его статусом (``webhook.status``).
Треды других поверхностей, интерактивных сессий и SDK-приложений на общем
хранилище (какой бы ни был ``system``) и задачи swarm (их песочницы ведёт
монитор swarm) не трогаются.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.threads.errors import OperationMismatch
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
#: Попыток восстановления подряд (ход падает вместе с процессом), после
#: которых задача — ``failed`` с причиной ``recovery_exhausted``.
MAX_RECOVERY_ATTEMPTS = 3
_FINAL = frozenset({"completed", "failed", "cancelled"})
#: Сколько заявка инстанса на продолжение сбоя держит задачу за ним: его
#: сборка контекста укладывается в окно. Упал при сборке — после окна
#: задачу берёт следующий обход, следующей попыткой.
CLAIM_WINDOW_SEC = 120.0


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
    if (
        record.key.system in RESERVED_SYSTEMS
        or not task_status.is_task(record)
        or _SWARM in record.frontend
    ):
        return False
    if record.lease is not None and not lease_expired(record.lease, now):
        return False
    done = max(record.state.inbox_seq, _abandoned_upto(record))
    pending = record.max_seq.get("inbox", 0) > done
    return record.runtime.turn == "running" or pending


def _abandoned_upto(record: ThreadRecord) -> int:
    """Граница входящих, на которых задача сдалась (финальный статус):
    такие не поднимаются, более поздние — поднимаются."""
    status = task_status.status_of(record) or {}
    upto = status.get("inbox_upto")
    if status.get("status") in _FINAL and type(upto) is int:
        return upto
    return 0


async def _acontinue(
    surface: WebhookSurface, threads: Threads, record: ThreadRecord
) -> bool:
    """Продолжить задачу ходом. Записи о прерванном ходе и счётчик
    попыток — только под бронью этого процесса: задачу, которую он же
    ведёт или строит, обход не трогает."""
    key = record.key
    status = task_status.status_of(record) or {}
    attempts = status.get("recovery_attempts")
    attempts = attempts if type(attempts) is int else 0
    # Ход сняла остановка сервера, а не сбой: попыткой это не считается.
    stopped = status.get("stopped_by") == "shutdown"

    async def prepare() -> bool:
        if _claimed_recently(status, record):
            return False  # в том числе последнюю попытку уже строит сосед
        if attempts >= MAX_RECOVERY_ATTEMPTS and not stopped:
            await _agive_up(surface, threads, record)
            return False
        if not await _aclaim(threads, record, status, attempts, stopped):
            return False
        if record.runtime.turn == "running":
            await _anote_interruption(threads, record)
            if status.get("status") == "running":
                surface.notify(key, {**status, "status": "interrupted"})
        return True

    return await surface.astart_recovered(
        key.system, key.thread, prepare=prepare
    )


def _crash_mark(record: ThreadRecord) -> str:
    """Сбой треда: поколение и номер последней записи истории."""
    return f"{record.generation}_{record.max_seq.get('messages', 0)}"


def _claimed_recently(status: dict, record: ThreadRecord) -> bool:
    """Заявка на продолжение этого же сбоя моложе окна: задачу строит
    другой инстанс (или этот же), захват треда ещё впереди."""
    claim = status.get("recovering")
    if not isinstance(claim, dict) or claim.get("mark") != _crash_mark(record):
        return False
    at = claim.get("at")
    return isinstance(at, (int, float)) and time.time() - at < CLAIM_WINDOW_SEC


async def _aclaim(
    threads: Threads,
    record: ThreadRecord,
    status: dict,
    attempts: int,
    stopped: bool,
) -> bool:
    """Выборы продолжающего и попытка — одной записью. ``op_id`` — от
    сбоя и прежней заявки: инстансы, прочитавшие один снимок, пишут одну
    операцию с разными заявками, и всем, кроме первого, хранилище
    отвечает ``OperationMismatch``."""
    key = record.key
    previous = status.get("recovering")
    token = previous.get("token") if isinstance(previous, dict) else None
    mark = _crash_mark(record)
    try:
        await task_status.aset_status(
            threads,
            key,
            op_id=f"recovery-claim:{key.system}:{key.thread}:{mark}:{token}",
            recovery_attempts=attempts if stopped else attempts + 1,
            stopped_by=None,
            recovering={
                "mark": mark,
                "at": time.time(),
                "token": uuid.uuid4().hex,
            },
        )
    except OperationMismatch:
        return False
    return True


async def _agive_up(
    surface: WebhookSurface, threads: Threads, record: ThreadRecord
) -> None:
    """Ход падал вместе с процессом раз за разом: задача — ``failed``,
    недоставленные входящие брошены, отметка хода снята."""
    await afinish_abandoned(
        surface,
        threads,
        record,
        status="failed",
        error="recovery_exhausted",
        op="recovery-exhausted",
    )


async def afinish_abandoned(
    surface: WebhookSurface,
    threads: Threads,
    record: ThreadRecord,
    *,
    status: str,
    op: str,
    inbox_upto: int | None = None,
    **fields: object,
) -> None:
    """Итог задачи, чей ход вести некому (инстанс упал): статус,
    недоставленные входящие брошены, отметка хода снята — восстановление
    тред больше не поднимает."""
    from gc_core.threads import runtime_rules
    from gc_core.threads.models import SideChange

    key = record.key
    final = {
        "status": status,
        **fields,
        "inbox_upto": record.max_seq.get("inbox", 0)
        if inbox_upto is None
        else inbox_upto,
    }
    await task_status.aset_status(threads, key, **final)
    runtime = runtime_rules.with_turn(record.runtime, running=False)
    await threads.store.append_side(
        key,
        SideChange(
            op_id=f"{op}:{key.system}:{key.thread}:{record.revision}",
            set_runtime=runtime,
        ),
    )
    surface.notify(key, {**(task_status.status_of(record) or {}), **final})


async def _anote_interruption(threads: Threads, record: ThreadRecord) -> None:
    """Техническое сообщение о сбое — во входящий поток, один раз на
    прерванный ход: инстансы, увидевшие тот же тред, пишут одну и ту же
    операцию (``op_id`` от поколения и номера последней записи истории)."""
    key = record.key
    mark = _crash_mark(record)
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
