"""Статус задачи вебхука — в хранилище, в состоянии фронтенда ``webhook``
треда: ``/runs`` отвечает одинаково на любом инстансе.

Поля: ``status`` (``running`` | ``completed`` | ``failed`` | ``cancelled``),
``run_id`` (прогон, который его записал), ``instance``, ``started_at``,
``finished_at``, ``result``, ``error``. Время — секунды эпохи, как раньше
отдавал реестр в памяти.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from fastapi.encoders import jsonable_encoder
from gc_core.threads.facade import Threads
from gc_core.threads.models import ThreadKey, ThreadRecord

FRONTEND = "webhook"


async def aset_status(threads: Threads, key: ThreadKey, **fields: Any) -> None:
    """Слить поля в статус задачи (атомарно в хранилище)."""
    await threads.update_frontend(key, FRONTEND, jsonable_encoder(fields))


def status_of(record: ThreadRecord | None) -> dict | None:
    """Статус задачи из записи треда; не было — ``None``."""
    if record is None:
        return None
    status = record.frontend.get(FRONTEND)
    if not isinstance(status, dict) or not status.get("status"):
        return None
    return status


def status_body(status: dict, record: ThreadRecord, base: dict) -> dict:
    """Ответ ``/runs`` в прежней форме."""
    body = {
        "status": status["status"],
        **base,
        "thread_id": record.key.thread,
        "started_at": status.get("started_at"),
    }
    if status["status"] == "running":
        body["last_activity_at"] = _last_activity(status, record)
        return body
    body["finished_at"] = status.get("finished_at")
    if status["status"] == "completed":
        body["result"] = status.get("result")
    if status["status"] == "failed":
        body["error"] = status.get("error")
    return body


def _last_activity(status: dict, record: ThreadRecord) -> Any:
    """Последняя активность — последняя запись в тред (шаг хода), но не
    раньше старта прогона."""
    started = status.get("started_at")
    updated = record.info.updated_at
    if not updated:
        return started
    try:
        moment = datetime.fromisoformat(updated).timestamp()
    except ValueError:
        return started
    if isinstance(started, (int, float)):
        return max(float(started), moment)
    return moment
