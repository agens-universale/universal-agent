"""Локальное относительное время для списка сессий (Spec 1, Rule A)."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Optional

_MONTHS_GEN = [
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
]

_MONTHS_SHORT = [
    "янв",
    "фев",
    "мар",
    "апр",
    "мая",
    "июн",
    "июл",
    "авг",
    "сен",
    "окт",
    "ноя",
    "дек",
]


def _now_local() -> datetime:
    """Единственная точка «сейчас» для часов хода (подмена в тестах)."""
    return datetime.now().astimezone()


def parse_iso_utc(value) -> Optional[datetime]:
    """ISO-таймстемп → aware datetime (нормализуя 'Z'); None при пустом/битом."""
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        dt = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo is not None else dt.replace(tzinfo=UTC)


def format_relative_local(
    iso_utc: str, *, now: Optional[datetime] = None
) -> str:
    """ISO-время (UTC) → русская относительная локальная строка.

    <1м «только что»; <60м «N мин назад»; <24ч «N ч назад»; далее по
    календарным локальным датам: вчера / N дн назад (2..6) / «D месяца[, ЧЧ:ММ]» /
    «D месяца ГГГГ». Кривой ввод возвращается как есть."""
    try:
        dt = datetime.fromisoformat((iso_utc or "").replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return iso_utc or ""
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    dt = dt.astimezone()  # в локальную зону
    now = now or datetime.now().astimezone()
    if now.tzinfo is None:
        now = now.astimezone()

    delta = (now - dt).total_seconds()
    if delta < 0:
        delta = 0
    if delta < 60:
        return "только что"
    if delta < 3600:
        return f"{int(delta // 60)} мин назад"
    if delta < 86400:
        return f"{int(delta // 3600)} ч назад"

    days = (now.date() - dt.date()).days
    hm = dt.strftime("%H:%M")
    if days == 1:
        return f"вчера, {hm}"
    if 2 <= days <= 6:
        return f"{days} дн назад"
    md = f"{dt.day} {_MONTHS_GEN[dt.month - 1]}"
    if dt.year == now.year:
        return f"{md}, {hm}"
    return f"{md} {dt.year}"


def format_turn_clock(
    ended_at: Optional[str], *, now: Optional[datetime] = None
) -> str:
    """Локальное время конца хода: «14:32», «вчера 18:42», «17 окт 18:42»,
    «17 окт 2025 18:42». Пустое или кривое — пустая строка."""
    dt = parse_iso_utc(ended_at)
    if dt is None:
        return ""
    dt = dt.astimezone()
    now = (now or _now_local()).astimezone()
    hm = dt.strftime("%H:%M")
    days = (now.date() - dt.date()).days
    if days == 0:
        return hm
    if days == 1:
        return f"вчера {hm}"
    md = f"{dt.day} {_MONTHS_SHORT[dt.month - 1]}"
    if dt.year == now.year:
        return f"{md} {hm}"
    return f"{md} {dt.year} {hm}"
