"""Правила контракта хранилища тредов v2, общие для всех реализаций.

Аренда и фенсинг, порядок и курсор списка тредов. Реализации хранилищ зовут
эти функции, чтобы одно и то же правило не расходилось между базами.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import UTC, datetime

from gc_core.threads.errors import OwnershipError
from gc_core.threads.models import (
    JsonObject,
    Lease,
    LeaseRequest,
    Page,
    ThreadQuery,
    ThreadRecord,
)


def instant(stamp: str | None) -> datetime:
    """Момент времени из ISO-8601 (``Z`` или смещение); пусто — самое раннее."""
    if not stamp:
        return datetime.min.replace(tzinfo=UTC)
    return datetime.fromisoformat(stamp)


def lease_expired(lease: Lease, now: datetime) -> bool:
    return lease.expires_at is not None and lease.expires_at <= now


def granted_lease(
    current: Lease | None,
    last_epoch: int,
    request: LeaseRequest,
    now: datetime,
) -> Lease:
    """Аренда, которую выдаёт ``claim``; чужая живая — ``OwnershipError``.

    Своя живая — продление с той же эпохой. Истёкшая или снятая — новая эпоха
    (``last_epoch + 1``), даже для того же владельца: для фенсинга это смена.
    """
    expires = None if request.ttl is None else now + request.ttl
    if current is not None and not lease_expired(current, now):
        if current.owner != request.owner:
            raise OwnershipError(f"тред ведёт {current.owner!r}")
        epoch = current.epoch
    else:
        epoch = last_epoch + 1
    return Lease(owner=request.owner, epoch=epoch, expires_at=expires)


def check_writer(current: Lease | None, lease: Lease, now: datetime) -> None:
    """Пишущий ``commit`` держит текущую живую аренду — иначе отказ."""
    if current is None:
        raise OwnershipError("запись без claim")
    if (current.owner, current.epoch) != (lease.owner, lease.epoch):
        raise OwnershipError("аренда сменилась: запись по чужой эпохе")
    if lease_expired(current, now):
        raise OwnershipError("аренда истекла")


def same_lease(current: Lease | None, lease: Lease) -> bool:
    return current is not None and (current.owner, current.epoch) == (
        lease.owner,
        lease.epoch,
    )


def _matches(record: ThreadRecord, query: ThreadQuery) -> bool:
    updated = instant(record.info.updated_at)
    after, before = query.updated_after, query.updated_before
    checks = (
        query.system is None or record.key.system == query.system,
        query.workspace is None or record.info.workspace == query.workspace,
        after is None or updated > instant(after),
        before is None or updated < instant(before),
    )
    return all(checks)


def _after_cursor(record: ThreadRecord, cursor: str) -> bool:
    """Идёт ли запись после курсора в порядке (updated_at ↓, system, thread)."""
    updated, system, thread = cursor.split("|", 2)
    mine, theirs = instant(record.info.updated_at), instant(updated)
    if mine != theirs:
        return mine < theirs
    return (record.key.system, record.key.thread) > (system, thread)


def page_of(
    records: Iterable[ThreadRecord], query: ThreadQuery
) -> Page[ThreadRecord]:
    """Страница списка тредов: фильтр, порядок (updated_at ↓, system,
    thread), курсор — непрозрачная строка последней записи страницы."""
    chosen = [r for r in records if _matches(r, query)]
    chosen.sort(key=lambda r: (r.key.system, r.key.thread))
    chosen.sort(key=lambda r: instant(r.info.updated_at), reverse=True)
    if query.cursor:
        chosen = [r for r in chosen if _after_cursor(r, query.cursor)]
    page = chosen[: query.limit]
    cursor = None
    if len(chosen) > query.limit:
        last = page[-1]
        cursor = f"{last.info.updated_at}|{last.key.system}|{last.key.thread}"
    return Page[ThreadRecord](items=page, next_cursor=cursor)


def patched_frontend(
    current: JsonObject | None, patch: JsonObject
) -> JsonObject:
    """Состояние фронтенда после слияния ``patch`` — ``dict.update``, как
    всегда делал ``update_tui_meta``: ключ со значением ``None`` хранит null."""
    merged = copy.deepcopy(current or {})
    merged.update(copy.deepcopy(patch))
    return merged
