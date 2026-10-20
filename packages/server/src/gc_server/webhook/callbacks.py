"""Обратные вызовы о завершении задачи вебхука (спека сервера, 14.4).

Событие ``{id, type, timestamp, data: {system, task_id, status, result,
error, finished_at}}`` уходит POST-ом на ``webhook.callbacks.url`` или на
``callback_url`` запроса (только под ``allowed_urls``). Подпись — Standard
Webhooks: ``webhook-id`` — id события, одинаковый во всех повторах. Повторы
с паузами 1, 2, 4, 8, 16 с — до ``max_attempts``. Шлёт инстанс, который
довёл ход; упадёт он до доставки — события не будет (очереди в хранилище
нет).
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
import uuid
from collections.abc import Awaitable, Callable
from typing import Any
from urllib.parse import unquote, urlsplit

import httpx
from gc_core.execution.environment import utc_now
from gc_core.threads.models import ThreadKey

from gc_server.webhook.signatures import sign

logger = logging.getLogger(__name__)

_MAX_PAUSE = 16


def allowed_callback(url: str, allowed: list[str]) -> bool:
    """``url`` под одним из префиксов: схема, хост и порт — точно, путь —
    началом; логина в адресе, сегментов «.» и «..» (в том числе
    закодированных) и обратных слешей быть не должно — получатель
    нормализует путь, и префикс обходился бы."""
    target = urlsplit(url)
    if target.username or target.password or not target.hostname:
        return False
    if not _plain_path(target.path):
        return False
    for prefix in allowed:
        base = urlsplit(prefix)
        if (target.scheme, target.netloc) != (base.scheme, base.netloc):
            continue
        if target.path.startswith(base.path or "/"):
            return True
    return False


def _plain_path(path: str) -> bool:
    for _ in range(3):  # %252e → %2e → .
        decoded = unquote(path)
        if decoded == path:
            break
        path = decoded
    if "\\" in path or "%" in path:
        return False
    return not any(seg in (".", "..") for seg in path.split("/"))


def make_event(kind: str, key: ThreadKey, status: dict) -> dict:
    return {
        "id": "evt_" + uuid.uuid4().hex,
        "type": f"task.{kind}",
        "timestamp": utc_now().isoformat(),
        "data": {
            "system": key.system,
            "task_id": key.thread,
            "status": status.get("status"),
            "result": status.get("result"),
            "error": status.get("error"),
            "finished_at": status.get("finished_at"),
        },
    }


class CallbackSender:
    """Отправка событий в фоне с повторами."""

    def __init__(
        self,
        config: Any,
        *,
        client: httpx.AsyncClient | None = None,
        sleep: Callable[[float], Awaitable[Any]] = asyncio.sleep,
    ) -> None:
        self.config = config
        self.client = client
        self.sleep = sleep
        self._own_client = client is None
        self._tasks: set[asyncio.Task] = set()

    def send(self, url: str, event: dict) -> None:
        if event["type"] not in self.config.events:
            return
        task = asyncio.get_running_loop().create_task(
            self.adeliver(url, event)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def adeliver(self, url: str, event: dict) -> bool:
        body = json.dumps(event, ensure_ascii=False).encode()
        attempts = self.config.max_attempts
        for attempt in range(1, attempts + 1):
            if await self._apost(url, event["id"], body):
                return True
            if attempt < attempts:
                await self.sleep(min(2 ** (attempt - 1), _MAX_PAUSE))
        logger.warning(
            "callback %s не доставлен за %d попыток: %s",
            event.get("type"),
            attempts,
            url,
        )
        return False

    async def _apost(self, url: str, event_id: str, body: bytes) -> bool:
        headers = {"content-type": "application/json", "webhook-id": event_id}
        secrets = self.config.signing_secret or []
        if secrets:
            stamp = int(time.time())
            headers["webhook-timestamp"] = str(stamp)
            headers["webhook-signature"] = " ".join(
                sign(secret, event_id, stamp, body) for secret in secrets
            )
        try:
            response = await self._client().post(
                url, content=body, headers=headers
            )
        except httpx.HTTPError:
            return False
        return 200 <= response.status_code < 300

    def _client(self) -> httpx.AsyncClient:
        if self.client is None:
            self.client = httpx.AsyncClient(timeout=10.0)
        return self.client

    async def adrain(self) -> None:
        """Дождаться отправок в полёте."""
        if self._tasks:
            await asyncio.gather(*self._tasks, return_exceptions=True)

    async def aclose(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        await self.adrain()
        if self._own_client and self.client is not None:
            await self.client.aclose()
            self.client = None
