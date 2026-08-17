"""Общие хелперы счётчика уведомлений для карточки и футера TUI."""
from __future__ import annotations

from typing import List, Tuple

from gc_core.startup_notices import Notice


def notice_counts(notices: List[Notice]) -> Tuple[int, int]:
    warns = sum(1 for n in notices if n.level == "warning")
    infos = sum(1 for n in notices if n.level == "info")
    return warns, infos


def format_notices_badge(notices: List[Notice], *, with_hint: bool = True) -> str:
    """Строка вида «⚠ 2 · ℹ 1 · F4/notices»; пустая, если уведомлений нет."""
    warns, infos = notice_counts(notices)
    if not warns and not infos:
        return ""
    parts: list[str] = []
    if warns:
        parts.append(f"⚠ {warns}")
    if infos:
        parts.append(f"ℹ {infos}")
    if with_hint:
        parts.append("F4/notices")
    return " · ".join(parts)


def format_notices_footer_suffix(notices: List[Notice]) -> str:
    """Суффикс для footer-left: «  ·  ⚠ 1 · F4/notices»."""
    badge = format_notices_badge(notices)
    return f"  ·  {badge}" if badge else ""
