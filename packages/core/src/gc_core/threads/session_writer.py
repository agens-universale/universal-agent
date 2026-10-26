"""Запись одной сессии треда поверх ``ThreadStore`` v2.

Аренда, ревизия и повторы — здесь, а не у каждого пишущего:

* аренда берётся явно (``aclaim``) или лениво, на первой записи, и
  продлевается, когда до истечения остаётся меньше половины срока;
  бессрочная (владелец — workflow Temporal) не продлевается и не снимается;
* **потерянная аренда не берётся заново**: чужая запись по нашей аренде
  или конфликт ревизии — ``ThreadBusy``, ход останавливается и поверх чужого
  хода не пишет;
* ревизия — из последней квитанции, тред перед записью не перечитывается;
* временный сбой хранилища (``StoreUnavailable``, ``OSError``, таймаут) —
  повтор той же записи с тем же ``op_id``: квитанция не даст задвоить.

Код исполняется и в workflow Temporal: ``op_id`` и время — из среды
исполнения (там они детерминированы).
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any, TypeVar

from gc_core.execution.environment import utc_now
from gc_core.threads.errors import (
    OwnershipError,
    StoreUnavailable,
    ThreadBusy,
    ThreadConflict,
    ThreadStoreError,
)
from gc_core.threads.models import (
    Lease,
    LeaseRequest,
    Receipt,
    SideChange,
    ThreadChange,
    ThreadKey,
    ThreadRecord,
)
from gc_core.threads.store import ThreadStore
from gc_core.utils.generate_message_id import generate_msg_id_sha256

T = TypeVar("T")
#: Срок аренды сессии: процесс, упавший посреди хода, держит тред не дольше.
LEASE_TTL = timedelta(seconds=60)
_log = logging.getLogger(__name__)


@dataclass(frozen=True)
class RetryPolicy:
    """Повтор записи при временной недоступности хранилища."""

    max_attempts: int = 5
    initial_delay_sec: float = 1.0
    backoff_factor: float = 2.0

    @classmethod
    def from_settings(cls, settings: Any) -> RetryPolicy:
        """Из ``thread_store.retry`` настроек (нет настроек — умолчания)."""
        from gc_core.settings import settings_of

        try:
            retry = settings_of(settings).thread_store.retry
        except Exception:  # без настроек — умолчания
            return cls()
        return cls(
            max_attempts=retry.max_attempts,
            initial_delay_sec=retry.initial_delay_sec,
            backoff_factor=retry.backoff_factor,
        )


def transient(exc: BaseException) -> bool:
    """Сбой, после которого запись можно повторить с тем же ``op_id``."""
    if isinstance(exc, StoreUnavailable):
        return True
    if isinstance(exc, ThreadStoreError):
        return False
    return isinstance(exc, (OSError, TimeoutError))


def _op_id() -> str:
    return generate_msg_id_sha256("session")


class SessionWriter:
    """Аренда, ревизия и записи одной сессии треда ``key``."""

    def __init__(
        self,
        store: ThreadStore,
        key: ThreadKey,
        *,
        owner: str,
        ttl: timedelta | None = LEASE_TTL,
        retry: RetryPolicy | None = None,
        lease: Lease | None = None,
        clock: Callable[[], datetime] = utc_now,
    ) -> None:
        self.store = store
        self.key = key
        self.owner = owner
        self.ttl = ttl
        self.retry = retry or RetryPolicy()
        self.lease = lease
        self.revision = 0
        self._clock = clock
        #: Аренду потеряли: дальше — только ``ThreadBusy``, без перезахвата.
        self._lost = False
        #: Свою аренду со сроком отпустили; следующая запись хода возьмёт
        #: тред заново.
        self._released = False

    @property
    def gave_up_lease(self) -> bool:
        """Тред этой сессией отпущен или потерян: его может вести другой
        ход, и runtime треда теперь его."""
        return self._lost or self._released

    @property
    def renews(self) -> bool:
        """Аренда со сроком — её продлевают, пока идёт ход."""
        return self.ttl is not None

    # --- повтор ---------------------------------------------------------------

    async def retried(self, call: Callable[[], Awaitable[T]], what: str) -> T:
        from gc_core.utils.aretry import retry_with_backoff_async

        policy = self.retry

        # Отказ по аренде или ревизии — ожидаемый исход (ThreadBusy у
        # вызывающего), не сбой: ERROR в лог — только когда исчерпаны
        # повторы временного сбоя хранилища.
        @retry_with_backoff_async(
            max_attempts=policy.max_attempts,
            initial_delay_sec=policy.initial_delay_sec,
            backoff_factor=policy.backoff_factor,
            should_retry=transient,
            no_more_retries_log_level=logging.DEBUG,
            operation_name=what,
        )
        async def once() -> T:
            return await call()

        try:
            return await once(use_retry=True, logger=_log)
        except Exception as exc:
            if transient(exc):
                _log.error(
                    "%s failed after %s attempts: %r",
                    what,
                    policy.max_attempts,
                    exc,
                )
            raise

    # --- аренда ---------------------------------------------------------------

    async def _busy(self, exc: Exception) -> ThreadBusy:
        self._lost = True
        self.lease = None
        try:
            record = await self.store.get_thread(self.key)
        except Exception:  # владелец — только для текста
            record = None
        owner = record.lease.owner if record and record.lease else None
        return ThreadBusy(
            f"тред {self.key.system}/{self.key.thread} занят: его ведёт "
            f"{owner or 'другая сессия'} ({exc})",
            owner=owner,
        )

    async def aclaim(self) -> ThreadRecord | None:
        """Взять тред для хода; чужая живая аренда — ``ThreadBusy``.
        Бессрочная аренда, уже взятая владельцем (workflow), — как есть,
        без обращения к хранилищу (``None``)."""
        if self._lost:
            raise await self._busy(OwnershipError("аренда потеряна"))
        if self.lease is not None and self.lease.expires_at is None:
            return None
        held = self.lease
        request = LeaseRequest(owner=self.owner, ttl=self.ttl)
        try:
            record = await self.retried(
                lambda: self.store.claim(self.key, request), "thread claim"
            )
        except OwnershipError as exc:
            raise await self._busy(exc) from None
        if self._lapsed_under_foreign_turn(held, record):
            # Своя аренда истекла, и тред успел записать другой ход:
            # продолжать на его ревизии — затереть его работу. Тред — назад.
            await self._arelease_quietly(record.lease)
            raise await self._busy(
                OwnershipError("аренда истекла, тред записал другой ход")
            )
        self.lease = record.lease
        self.revision = record.revision
        self._released = False
        return record

    def _lapsed_under_foreign_turn(
        self, held: Lease | None, record: ThreadRecord
    ) -> bool:
        """Продление дало новую эпоху (своя аренда истекла), а ревизия треда
        — не та, что писал этот ход."""
        return (
            held is not None
            and record.lease is not None
            and record.lease.epoch != held.epoch
            and record.revision != self.revision
        )

    async def _arelease_quietly(self, lease: Lease | None) -> None:
        if lease is None:
            return
        try:
            await self.store.release(self.key, lease)
        except Exception:  # аренда истечёт сама; отказ важнее
            _log.warning(
                "thread release after lost lease failed", exc_info=True
            )

    async def arenew(self) -> None:
        """Продлить свою аренду со сроком; её перехватили — ``ThreadBusy``."""
        if self.lease is None or self.lease.expires_at is None:
            return
        await self.aclaim()

    async def _aensure_lease(self) -> None:
        if self.lease is None:
            await self.aclaim()
            return
        expires = self.lease.expires_at
        if (
            expires is not None
            and self.ttl is not None
            and expires - self._clock() <= self.ttl / 2
        ):
            await self.aclaim()

    async def arelease(self) -> None:
        """Отпустить свою аренду со сроком; бессрочную снимает её владелец."""
        lease = self.lease
        if lease is None or lease.expires_at is None:
            return
        self.lease = None
        self._released = True
        await self.retried(
            lambda: self.store.release(self.key, lease), "thread release"
        )

    # --- запись ---------------------------------------------------------------

    async def acommit(self, **fields: Any) -> Receipt:
        """Запись хода по своей аренде и ревизии; потеря аренды или чужая
        запись — ``ThreadBusy`` (без повтора и без перезахвата)."""
        await self._aensure_lease()
        change = ThreadChange(
            op_id=_op_id(),
            expected_revision=self.revision,
            lease=self.lease,
            **fields,
        )
        try:
            receipt = await self.retried(
                lambda: self.store.commit(self.key, change), "thread commit"
            )
        except (OwnershipError, ThreadConflict) as exc:
            raise await self._busy(exc) from None
        self.revision = receipt.revision
        if receipt.lease is None:
            # Сброс истории снимает аренду: дальше пишет новый захват.
            self.lease = None
        return receipt

    async def aside(self, **fields: Any) -> Receipt:
        """Запись сбоку (runtime, потоки) по своей аренде, без ревизии."""
        if self.lease is not None:
            # Истёкшая аренда после чужого хода — отказ, а не перезапись
            # его runtime (запись сбоку хранилище по сроку не фенсит).
            await self._aensure_lease()
        change = SideChange(op_id=_op_id(), lease=self.lease, **fields)
        try:
            return await self.retried(
                lambda: self.store.append_side(self.key, change),
                "thread side write",
            )
        except OwnershipError as exc:
            raise await self._busy(exc) from None
