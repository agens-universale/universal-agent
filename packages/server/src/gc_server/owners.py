"""Реестр владельцев: пульс инстанса и его задачи «в работе» на тредах.

У каждого запуска процесса — свой тред-реестр ``gc_owners/<ключ>``: его
аренда (``claim`` со сроком, продление на трети срока) — пульс процесса,
секция фронтенда — задачи, которые процесс ведёт. Тред-каталог
``gc_owners/dir`` перечисляет реестры. Восстановление читает каталог и
записи мёртвых реестров (аренда истекла или отпущена), а не все треды:
O(инстансов), а не O(истории). Контракт хранилища тот же — только
``claim``, ``release``, запись сбоку и ``delete``.

Мёртвый реестр разбирает один инстанс: он берёт его своей арендой. Владелец,
чей реестр взяли или удалили (продление опоздало), узнаёт об этом
(``on_lost``) и заводит новый.
"""

from __future__ import annotations

import asyncio
import logging
import re
import uuid
from collections.abc import Awaitable, Callable, Iterable
from datetime import datetime, timedelta
from typing import Any

from gc_core.execution.environment import utc_now
from gc_core.threads.errors import OwnershipError, ThreadConflict
from gc_core.threads.facade import Threads
from gc_core.threads.instance import instance_id
from gc_core.threads.models import (
    Lease,
    LeaseRequest,
    SideChange,
    ThreadKey,
    ThreadRecord,
)
from gc_core.threads.rules import lease_expired

logger = logging.getLogger(__name__)

SYSTEM = "gc_owners"
DIRECTORY = ThreadKey(system=SYSTEM, thread="dir")
DIRECTORY_SECTION = "owners"
#: Срок аренды реестра — сколько процесс считается живым без продления.
TTL = timedelta(seconds=60)
#: Продление — на трети срока: два сбоя подряд ещё не потеря.
RENEW_SECONDS = 20.0
#: Снятая запись — null в секции (контракт слияния). Свой реестр процесс
#: переписывает целиком из памяти после стольких снятий.
COMPACT_AFTER = 64
#: Вычеркнутых ключей в каталоге (по одному на запуск процесса), после
#: которых восстановитель переписывает каталог.
DIRECTORY_COMPACT_AT = 256
#: Раз в столько продлений (~5 мин) живой реестр вписывает себя в каталог
#: заново: переписывание каталога могло совпасть с его записью.
REASSERT_EVERY = 15

Lost = Callable[[], Awaitable[None]]


def new_key(instance: str | None = None) -> str:
    """Ключ реестра запуска: имя инстанса (только ``[A-Za-z0-9_-]``, как
    id треда) и случайный хвост — рестарт не совпадает с прежним."""
    base = re.sub(r"[^A-Za-z0-9_-]+", "_", instance or instance_id())
    return f"{base.strip('_') or 'gc'}-{uuid.uuid4().hex[:12]}"


def registry_key(key: str) -> ThreadKey:
    return ThreadKey(system=SYSTEM, thread=key)


def entries(record: ThreadRecord | None, section: str) -> dict[str, Any]:
    """Записи реестра в секции ``section`` (снятые — без ``None``)."""
    if record is None:
        return {}
    found = record.frontend.get(section) or {}
    return {k: v for k, v in found.items() if isinstance(v, dict)}


class OwnerRegistry:
    """Свой реестр процесса: заводится при первой записи, продлевается в
    фоне, отпускается при остановке."""

    def __init__(
        self,
        threads: Threads,
        *,
        section: str,
        on_lost: Lost | None = None,
        ttl: timedelta = TTL,
        renew_seconds: float = RENEW_SECONDS,
    ) -> None:
        self._threads = threads
        self._section = section
        self.on_lost = on_lost
        self._ttl = ttl
        self._renew_seconds = renew_seconds
        self.key: str | None = None
        self._lease: Lease | None = None
        self._generation = 0
        self._since = ""
        self._live: dict[str, dict[str, Any]] = {}
        self._removed = 0
        self._renewals = 0
        self._keeper: asyncio.Task | None = None
        self._lock = asyncio.Lock()

    @property
    def ttl(self) -> timedelta:
        """Срок аренды реестра — им же восстановитель берёт мёртвые."""
        return self._ttl

    async def aensure(self) -> str:
        """Ключ своего реестра; нет — завести (аренда, запись в каталог)."""
        async with self._lock:
            if self.key is not None:
                return self.key
            key = new_key()
            record = await self._threads.store.claim(
                registry_key(key), LeaseRequest(owner=key, ttl=self._ttl)
            )
            self._since = _now()
            try:
                await self._threads.update_frontend(
                    DIRECTORY, DIRECTORY_SECTION, {key: {"since": self._since}}
                )
            except BaseException:
                # Реестр вне каталога никто не найдёт: аренду — назад.
                await self._arelease_quietly(key, record.lease)
                raise
            self._lease, self._generation = record.lease, record.generation
            self._live, self._removed, self._renewals = {}, 0, 0
            self.key = key
            self._keeper = asyncio.create_task(self._akeep(key))
            return key

    async def aadd(self, entry_id: str, data: dict[str, Any]) -> str:
        """Запись «в работе»; возвращает ключ реестра (для отметок).

        Аренда на исходе (процесс стоял) — сперва продлить: запись в реестр,
        который уже разобрали и удалили, воскресила бы его вне каталога."""
        key = await self.aensure()
        if self._expiring():
            await self._arenew()
            key = await self.aensure()
        self._live[entry_id] = data
        await self._threads.update_frontend(
            registry_key(key), self._section, {entry_id: data}
        )
        return key

    async def aremove(self, entry_id: str) -> None:
        key = self.key
        if key is None:
            return
        self._live.pop(entry_id, None)
        self._removed += 1
        await self._threads.update_frontend(
            registry_key(key), self._section, {entry_id: None}
        )
        if self._removed < COMPACT_AFTER:
            return
        self._removed = 0
        record = await self._threads.get(registry_key(key))
        if record is not None:
            await _acompact(self._threads, record, self._section)

    def _expiring(self) -> bool:
        lease = self._lease
        return lease is None or lease_expired(lease, utc_now() + self._ttl / 2)

    async def astop(self) -> None:
        """Остановка процесса: продление — стоп, аренда — отпустить, чтобы
        задачи разобрали сразу, а не через срок."""
        keeper, self._keeper = self._keeper, None
        if keeper is not None:
            keeper.cancel()
            await asyncio.gather(keeper, return_exceptions=True)
        key, lease = self.key, self._lease
        self.key = self._lease = None
        if key is not None and lease is not None:
            await self._arelease_quietly(key, lease)

    async def _arelease_quietly(self, key: str, lease: Lease | None) -> None:
        if lease is None:
            return
        try:
            await self._threads.store.release(registry_key(key), lease)
        except Exception:  # аренда истечёт сама
            logger.warning("owners: аренда реестра не отпущена", exc_info=True)

    async def _adrop_revived(self, key: str, lease: Lease | None) -> None:
        await self._arelease_quietly(key, lease)
        try:
            await self._threads.store.delete(
                registry_key(key), op_id=f"owners-revived-{uuid.uuid4().hex}"
            )
        except Exception:  # пустой тред без каталога вреда не несёт
            logger.warning("owners: пустой реестр не удалён", exc_info=True)

    async def _akeep(self, key: str) -> None:
        """Продление своего ключа; заведён новый реестр — этот цикл ушёл.
        Сбой — повтор на следующем шаге: потерю решает срок аренды."""
        while self.key == key:
            await asyncio.sleep(self._renew_seconds)
            if self.key != key:
                return
            try:
                await self._arenew()
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.warning("owners: продление реестра", exc_info=True)

    async def _arenew(self) -> None:
        key = self.key
        if key is None:
            return
        try:
            record = await self._threads.store.claim(
                registry_key(key), LeaseRequest(owner=key, ttl=self._ttl)
            )
        except OwnershipError:
            await self._alost()
            return
        except Exception:
            if self._lease is None or lease_expired(self._lease, utc_now()):
                await self._alost()
            else:
                logger.warning("owners: продление не удалось", exc_info=True)
            return
        held = self._lease
        if record.generation != self._generation:
            # Реестр разобрали и удалили — продление завело пустой тред:
            # убрать его, реестр уже не наш.
            await self._adrop_revived(key, record.lease)
            await self._alost()
            return
        if held is not None and (
            record.lease.epoch != held.epoch
            or record.lease.token != held.token
        ):
            # Аренда истекала — реестр мог взять восстановитель (и разобрать
            # часть записей): он уже не наш.
            await self._arelease_quietly(key, record.lease)
            await self._alost()
            return
        self._lease = record.lease
        self._renewals += 1
        if self._renewals % REASSERT_EVERY == 0:
            try:
                await self._threads.update_frontend(
                    DIRECTORY, DIRECTORY_SECTION, {key: {"since": self._since}}
                )
            except Exception:  # следующая попытка — через REASSERT_EVERY
                logger.warning("owners: запись в каталог", exc_info=True)

    async def _alost(self) -> None:
        self.key = self._lease = None
        self._keeper = None
        if self.on_lost is not None:
            await self.on_lost()


async def adead(threads: Threads, *, own: str | None, now: datetime) -> list:
    """Ключи мёртвых реестров (аренда истекла или отпущена), кроме своего.
    Ключ каталога без треда-реестра — вычеркнуть."""
    directory = await threads.get(DIRECTORY)
    keys = list(entries(directory, DIRECTORY_SECTION))
    await _acompact_directory(threads, directory)
    dead = []
    for key in keys:
        if key == own:
            continue
        record = await threads.get(registry_key(key))
        if record is None:
            await threads.update_frontend(
                DIRECTORY, DIRECTORY_SECTION, {key: None}
            )
        elif record.lease is None or lease_expired(record.lease, now):
            dead.append(key)
    return dead


async def _acompact_directory(
    threads: Threads, directory: ThreadRecord | None
) -> None:
    """Каталог без вычеркнутых ключей; свежая запись не затирается."""
    if directory is None:
        return
    raw = directory.frontend.get(DIRECTORY_SECTION) or {}
    if sum(value is None for value in raw.values()) < DIRECTORY_COMPACT_AT:
        return
    await _acompact(threads, directory, DIRECTORY_SECTION)


async def _acompact(
    threads: Threads, record: ThreadRecord, section: str
) -> None:
    """Уплотняем сохранённый снимок, только пока он актуален.

    Конкурентная запись выигрывает; уборка повторится в следующий раз.
    Это относится и к общему каталогу, где замок процесса не помогает.
    """
    try:
        await threads.store.append_side(
            record.key,
            SideChange(
                op_id=f"owners-compact-{uuid.uuid4().hex}",
                expected_frontend=record.frontend,
                set_frontend=(section, entries(record, section)),
            ),
        )
    except ThreadConflict:
        pass


async def aclaim_dead(
    threads: Threads, key: str, *, owner: str, ttl: timedelta = TTL
) -> ThreadRecord | None:
    """Взять мёртвый реестр для разбора; его уже разбирает другой —
    ``None``."""
    try:
        return await threads.store.claim(
            registry_key(key), LeaseRequest(owner=owner, ttl=ttl)
        )
    except OwnershipError:
        return None


async def aforget(
    threads: Threads, key: str, *, section: str, entry_ids: Iterable[str]
) -> None:
    """Снять разобранные записи (остальные ждут следующего прохода)."""
    patch = dict.fromkeys(entry_ids)
    if patch:
        await threads.update_frontend(registry_key(key), section, patch)


async def adrop(threads: Threads, key: str) -> None:
    """Реестр разобран: удалить его и вычеркнуть из каталога."""
    await threads.store.delete(registry_key(key), op_id=f"owners-drop-{key}")
    await threads.update_frontend(DIRECTORY, DIRECTORY_SECTION, {key: None})


def _now() -> str:
    return utc_now().isoformat()
