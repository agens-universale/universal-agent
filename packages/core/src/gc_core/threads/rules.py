"""Правила контракта хранилища тредов v2, общие для всех реализаций.

Аренда и фенсинг, порядок и курсор списка тредов. Реализации хранилищ зовут
эти функции, чтобы одно и то же правило не расходилось между базами.
"""

from __future__ import annotations

import copy
from collections.abc import Iterable
from datetime import UTC, datetime

from gc_core.threads.errors import InvalidCursor, OwnershipError
from gc_core.threads.models import (
    INBOX,
    JsonObject,
    Lease,
    LeaseRequest,
    Page,
    SideChange,
    ThreadInfo,
    ThreadKey,
    ThreadQuery,
    ThreadRecord,
)

_EARLIEST = datetime.min.replace(tzinfo=UTC)


def instant(stamp: str | None) -> datetime:
    """Момент времени из ISO-8601 (``Z`` или смещение); пусто — самое раннее."""
    if not stamp:
        return _EARLIEST
    return datetime.fromisoformat(stamp)


def _updated(record: ThreadRecord) -> datetime:
    """Когда тред обновлён — для порядка списка. Время не по ISO (meta,
    испорченная чужим писателем) — самое раннее: тред в конце списка, а не
    отказ всего списка; без смещения — UTC."""
    stamp = record.info.updated_at
    if not isinstance(stamp, str):
        return _EARLIEST
    try:
        moment = instant(stamp)
    except ValueError:
        return _EARLIEST
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


def lease_expired(lease: Lease, now: datetime) -> bool:
    return lease.expires_at is not None and lease.expires_at <= now


def check_deletable(lease: Lease | None) -> None:
    """Тред, который ведёт durable-исполнитель (бессрочная аренда), не
    удаляет никто другой: ни уборщик по сроку, ни фронтенд."""
    if lease is not None and lease.expires_at is None:
        raise OwnershipError(
            f"тред ведёт durable-исполнитель {lease.owner!r}: удалить нельзя"
        )


def side_is_activity(change: SideChange) -> bool:
    """Запись сбоку, которая двигает ``updated_at``: сообщение во входящий
    поток — новая работа для треда (обход «обновлённых за окно» и чистка
    по сроку должны его видеть)."""
    return any(stream == INBOX for stream, _items in change.append)


def info_after_set(current: ThreadInfo, new: ThreadInfo) -> ThreadInfo:
    """``set_info`` заменяет карточку целиком, кроме заголовка: его ставит
    фронтенд, и запись хода без ``title`` (поле не задано явно) не стирает
    переименование треда."""
    if "title" in new.model_fields_set or "title" not in (
        current.model_fields_set
    ):
        return new
    kept = new.model_copy(update={"title": current.title})
    kept.__pydantic_fields_set__.add("title")
    return kept


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


def touches_turn(change: SideChange) -> bool:
    """Запись сбоку касается хода (статус, потоки, алиасы), а не только
    состояния фронтенда."""
    return bool(
        change.set_runtime is not None or change.append or change.add_aliases
    )


def check_side_writer(current: Lease | None, change: SideChange) -> None:
    """Запись хода сбоку в тред durable-исполнителя (бессрочная аренда) —
    только по его аренде; состояние фронтенда и треды без такой аренды —
    без проверки (``append_side`` не конфликтует с ходом)."""
    if current is None or current.expires_at is not None:
        return
    if touches_turn(change) and not (
        change.lease is not None and same_lease(current, change.lease)
    ):
        raise OwnershipError(
            f"тред ведёт durable-исполнитель {current.owner!r}: статус хода, "
            "потоки и алиасы пишет только он"
        )


def same_lease(current: Lease | None, lease: Lease) -> bool:
    return current is not None and (current.owner, current.epoch) == (
        lease.owner,
        lease.epoch,
    )


def _matches(record: ThreadRecord, query: ThreadQuery) -> bool:
    updated = _updated(record)
    after, before = query.updated_after, query.updated_before
    checks = (
        query.system is None or record.key.system == query.system,
        query.workspace is None or record.info.workspace == query.workspace,
        after is None or updated > instant(after),
        before is None or updated < instant(before),
    )
    return all(checks)


def parse_list_cursor(cursor: str) -> tuple[datetime, str, str]:
    """Курсор списка тредов (``updated_at|system|thread``, его пишут
    ``page_of`` и хранилища) → момент, система, тред; чужой или испорченный —
    ``InvalidCursor``."""
    try:
        updated, system, thread = cursor.split("|", 2)
        moment = datetime.fromisoformat(updated.replace("Z", "+00:00"))
        if moment.tzinfo is None:
            moment = moment.replace(tzinfo=UTC)
        # Момент — в UTC сразу: на краю диапазона (0001-01-01+14:00) перевод
        # переполняется здесь, а не в базе (500 вместо 400).
        moment = moment.astimezone(UTC)
        ThreadKey(system=system, thread=thread)
    except (ValueError, OverflowError):  # ValidationError — тоже ValueError
        raise InvalidCursor(f"негодный курсор списка: {cursor!r}") from None
    # Курсор пишут хранилища по записям: тредов до 1970 года и имён с NUL не
    # бывает — такой курсор подделан.
    if moment < _EPOCH or "\x00" in system + thread:
        raise InvalidCursor(f"негодный курсор списка: {cursor!r}")
    return moment, system, thread


_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)


def _after_cursor(
    record: ThreadRecord, cursor: tuple[datetime, str, str]
) -> bool:
    """Идёт ли запись после курсора в порядке (updated_at ↓, system, thread)."""
    theirs, system, thread = cursor
    mine = _updated(record)
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
    chosen.sort(key=_updated, reverse=True)
    if query.cursor:
        position = parse_list_cursor(query.cursor)
        chosen = [r for r in chosen if _after_cursor(r, position)]
    page = chosen[: query.limit]
    more = len(chosen) > query.limit
    cursor = list_cursor_of(page[-1]) if more else None
    return Page[ThreadRecord](items=page, next_cursor=cursor)


def list_cursor_of(record: ThreadRecord) -> str:
    """Курсор списка тредов «после этой записи»: тот же формат у всех
    хранилищ (его разбирает ``parse_list_cursor``)."""
    stamp = _updated(record).isoformat()
    return f"{stamp}|{record.key.system}|{record.key.thread}"


def patched_frontend(
    current: JsonObject | None, patch: JsonObject
) -> JsonObject:
    """Состояние фронтенда после слияния ``patch`` — ``dict.update``, как
    всегда делал ``update_tui_meta``: ключ со значением ``None`` хранит null."""
    merged = copy.deepcopy(current or {})
    merged.update(copy.deepcopy(patch))
    return merged
