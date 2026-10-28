"""Страница истории задачи для API ``/v1`` — по непрозрачному курсору.

Курсор — поколение треда, версия истории и последний отданный ``seq``.
Сжатие заменяет историю (новая версия, нумерация с начала), удаление и
пересоздание — поколение: старый курсор тогда не может продолжить ни
номером, ни смещением — ответ несёт ``reset`` и историю новой версии с
начала.
"""

from __future__ import annotations

import base64
import json
from dataclasses import dataclass
from typing import Any

from gc_core.threads.models import ThreadRecord

HISTORY = "messages"


@dataclass(frozen=True)
class Position:
    generation: int
    history_version: int
    seq: int


@dataclass(frozen=True)
class HistoryPage:
    entries: list[dict[str, Any]]
    position: Position
    reset: bool = False


def encode_cursor(position: Position) -> str:
    raw = json.dumps(
        {
            "g": position.generation,
            "h": position.history_version,
            "s": position.seq,
        },
        separators=(",", ":"),
    ).encode("ascii")
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def decode_cursor(text: str) -> Position:
    """Позиция из курсора; негодный — ``ValueError``."""
    try:
        padded = text + "=" * (-len(text) % 4)
        raw = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        values = (raw["g"], raw["h"], raw["s"])
    except (ValueError, KeyError, TypeError) as exc:
        # base64 и UTF-8 сюда же: binascii.Error и UnicodeError — ValueError.
        raise ValueError("broken cursor") from exc
    if not all(type(v) is int and v >= 0 for v in values):
        raise ValueError("broken cursor")
    return Position(*values)


async def ahistory_page(
    store: Any, record: ThreadRecord, position: Position | None, limit: int
) -> HistoryPage:
    """Записи истории после ``position`` (``None`` — с начала)."""
    reset = position is not None and (
        position.generation != record.generation
        or position.history_version != record.history_version
    )
    after = position.seq if position is not None and not reset else 0
    entries = await store.read_stream(
        record.key, HISTORY, after_seq=after, limit=limit
    )
    last = entries[-1].seq if entries else after
    return HistoryPage(
        [{"seq": e.seq, "ts": e.ts, "message": e.data} for e in entries],
        Position(record.generation, record.history_version, last),
        reset,
    )
