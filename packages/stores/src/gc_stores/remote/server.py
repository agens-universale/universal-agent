"""Сервер прокси одного треда — сторона процесса сервера.

На задачу swarm — свой unix-сокет в отдельном каталоге (его монтируют в
песочницу) и свой случайный токен. Проходят только операции хода над
тредом задачи: чтение записи и потоков, запись хода и сбоку, аренда,
вложения. Чужой тред, список, алиасы, удаление и обслуживание — отказ
``Forbidden``: утечка токена из песочницы даёт доступ только к своему
треду."""

from __future__ import annotations

import asyncio
import base64
import hmac
import os
import secrets
import shutil
import tempfile
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

from gc_core.threads.errors import StoreUnavailable, ThreadStoreError
from gc_core.threads.models import (
    BlobRef,
    Lease,
    LeaseRequest,
    SideChange,
    ThreadChange,
    ThreadKey,
)
from gc_core.threads.store import ThreadStore
from pydantic import ValidationError

from gc_stores.remote import protocol
from gc_stores.remote.protocol import wire

SOCKET_NAME = "thread.sock"
#: Соединений разом: песочница — недоверенный клиент, её запросы держат
#: кадр в памяти сервера.
MAX_CONNECTIONS = 4
#: Открытых соединений разом, с ждущими слота: сверх — закрываются сразу,
#: сокеты процесса сервера песочница не исчерпает.
MAX_OPEN = 16
#: Самая долгая аренда хода: ход берёт 60 с и продлевает её на половине.
MAX_LEASE_TTL = timedelta(minutes=15)
#: Сколько ждать строку запроса и отдачу ответа: молчащее или не
#: читающее соединение не держит слот.
READ_TIMEOUT_SECONDS = 30.0
#: Секции фронтенда, которые пишет ход (сессия ядра); остальные — сервера.
_SANDBOX_FRONTENDS = frozenset({"tui"})
#: Потоки ядра, которые пишет ход сбоку: трасса субагентов, журнал правок
#: и итоги хода. Входящие и отмены (``inbox``, ``control``) пишет сервер:
#: дописав себе сообщение, песочница продолжала бы себя мимо потолка задачи.
_SANDBOX_STREAMS = frozenset({"subagent_events", "edits", "turns"})
#: Короткий корень каталога сокета: путь unix-сокета ограничен ~104 байтами.
_SHORT_ROOT = "/tmp"

Op = Callable[["ThreadProxy", ThreadKey, dict[str, Any]], Awaitable[Any]]


class ThreadProxy:
    """Прокси треда ``key`` хранилища ``store``."""

    def __init__(self, store: ThreadStore, key: ThreadKey) -> None:
        self.store = store
        self.key = key
        self.token = secrets.token_hex(32)
        self.directory = tempfile.mkdtemp(
            prefix="gct-",
            dir=_SHORT_ROOT if os.path.isdir(_SHORT_ROOT) else None,
        )
        self.socket_path = os.path.join(self.directory, SOCKET_NAME)
        self._server: asyncio.AbstractServer | None = None
        self._slots = asyncio.Semaphore(MAX_CONNECTIONS)
        self._open = 0

    @classmethod
    async def astart(cls, store: ThreadStore, key: ThreadKey) -> ThreadProxy:
        proxy = cls(store, key)
        proxy._server = await asyncio.start_unix_server(
            proxy._handle, path=proxy.socket_path, limit=protocol.REQUEST_LIMIT
        )
        return proxy

    async def aclose(self) -> None:
        server, self._server = self._server, None
        if server is not None:
            server.close()
            await server.wait_closed()
        shutil.rmtree(self.directory, ignore_errors=True)

    async def _handle(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        self._open += 1
        try:
            if self._open <= MAX_OPEN:
                async with self._slots:
                    await self._serve(reader, writer)
        except (OSError, ValueError, asyncio.LimitOverrunError):
            pass  # клиент ушёл, молчит (TimeoutError — OSError) или прислал не кадр
        finally:
            self._open -= 1
            writer.close()

    async def _serve(
        self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter
    ) -> None:
        line = await asyncio.wait_for(reader.readline(), READ_TIMEOUT_SECONDS)
        reply = await self._reply(line)
        writer.write(protocol.encode(reply))
        await asyncio.wait_for(writer.drain(), READ_TIMEOUT_SECONDS)

    async def _reply(self, line: bytes) -> dict[str, Any]:
        try:
            request = protocol.decode(line)
        except ValueError:
            return _error("ValueError", "запрос — не строка JSON")
        if not hmac.compare_digest(
            str(request.get("token") or ""), self.token
        ):
            return _forbidden("токен не этой задачи")
        op = _OPS.get(str(request.get("op")))
        if op is None:
            return _forbidden(f"операция {request.get('op')!r} не хода")
        try:
            key = ThreadKey.model_validate(request.get("key"))
        except ValidationError:
            return _forbidden("нет ключа треда")
        if key != self.key:
            return _forbidden("тред не этой задачи")
        return await self._run(op, key, request.get("args") or {})

    async def _run(
        self, op: Op, key: ThreadKey, args: dict[str, Any]
    ) -> dict[str, Any]:
        try:
            return {"ok": await op(self, key, args)}
        except PermissionError as exc:
            return _forbidden(str(exc))
        except ThreadStoreError as exc:
            return _error(type(exc).__name__, str(exc))
        except (ValidationError, ValueError, TypeError, KeyError) as exc:
            return _error("ValueError", str(exc))
        except FileNotFoundError as exc:
            return _error("FileNotFoundError", str(exc))  # нет вложения
        except OSError as exc:
            return _error(
                "StoreUnavailable", str(StoreUnavailable.from_error(exc))
            )


def _error(kind: str, message: str) -> dict[str, Any]:
    return {"error": {"type": kind, "message": message}}


def _forbidden(message: str) -> dict[str, Any]:
    return _error(protocol.FORBIDDEN, message)


def _own_sections_only(change: SideChange) -> None:
    """Песочница пишет только то, что пишет ход: свою секцию фронтенда и
    потоки хода. Секции и потоки сервера (метка задачи swarm, статус
    вебхука, задачи A2A, входящие и отмены) — его: снятая метка отдала бы
    тред восстановлению вебхука, и ход пошёл бы на хосте мимо песочницы."""
    for section in (change.set_frontend, change.patch_frontend):
        if section is not None and section[0] not in _SANDBOX_FRONTENDS:
            raise PermissionError(f"секция фронтенда {section[0]!r} — сервера")
    for stream, _entries in change.append:
        own = (
            stream.split(".", 1)[0] in _SANDBOX_FRONTENDS
            if "." in stream
            else stream in _SANDBOX_STREAMS
        )
        if not own:
            raise PermissionError(f"поток {stream!r} — сервера")


def _no_aliases(change: ThreadChange | SideChange) -> None:
    """Алиасы — общее пространство имён сервера (ответы chat и др.)."""
    if getattr(change, "add_aliases", None):
        raise PermissionError("алиасы треда задаёт сервер")


async def _get_thread(proxy: ThreadProxy, key: ThreadKey, _args: dict) -> Any:
    record = await proxy.store.get_thread(key)
    return None if record is None else wire(record)


async def _read_stream(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    entries = await proxy.store.read_stream(
        key,
        str(args["stream"]),
        after_seq=int(args.get("after_seq") or 0),
        limit=args.get("limit"),
    )
    return [wire(entry) for entry in entries]


async def _read_archive(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    entries = await proxy.store.read_archive(key, str(args["archive_id"]))
    return [wire(entry) for entry in entries]


async def _commit(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    change = ThreadChange.model_validate(args["change"])
    _no_aliases(change)
    receipt = await proxy.store.commit(key, change)
    return wire(receipt)


async def _append_side(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    change = SideChange.model_validate(args["change"])
    _no_aliases(change)
    _own_sections_only(change)
    receipt = await proxy.store.append_side(key, change)
    return wire(receipt)


async def _claim(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    request = LeaseRequest.model_validate(args["request"])
    if request.ttl is None or request.ttl > MAX_LEASE_TTL:
        raise PermissionError(
            f"аренда без срока или дольше {MAX_LEASE_TTL} — только у сервера"
        )
    record = await proxy.store.claim(key, request)
    return wire(record)


async def _release(proxy: ThreadProxy, key: ThreadKey, args: dict) -> None:
    await proxy.store.release(key, Lease.model_validate(args["lease"]))


async def _put_blob(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    data = base64.b64decode(str(args["data"]), validate=True)
    ref = await proxy.store.put_blob(key, data, mime=str(args["mime"]))
    return wire(ref)


async def _get_blob(proxy: ThreadProxy, key: ThreadKey, args: dict) -> Any:
    ref = BlobRef.model_validate(args["ref"])
    if ref.key != key or os.path.basename(ref.name) != ref.name:
        raise PermissionError("вложение не этой задачи")
    return base64.b64encode(await proxy.store.get_blob(ref)).decode("ascii")


_OPS: dict[str, Op] = {
    "get_thread": _get_thread,
    "read_stream": _read_stream,
    "read_archive": _read_archive,
    "commit": _commit,
    "append_side": _append_side,
    "claim": _claim,
    "release": _release,
    "put_blob": _put_blob,
    "get_blob": _get_blob,
}

__all__ = ["SOCKET_NAME", "ThreadProxy"]
