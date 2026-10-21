"""Форматирование длительности и компактных чисел для отображения в TUI.

Чистая презентация: без зависимостей от контроллеров и доменных модулей.
"""

from __future__ import annotations


def _duration_parts(seconds: float) -> tuple[tuple[int, str], ...]:
    """(число, единица) длительности: до двух старших разрядов."""
    total = int(seconds) if seconds > 0 else 0
    if total < 60:
        return ((total, "с"),)
    if total < 3600:
        return ((total // 60, "м"), (total % 60, "с"))
    return ((total // 3600, "ч"), ((total % 3600) // 60, "м"))


def format_duration(seconds: float) -> str:
    """Человекочитаемая длительность.

    < 60     → "45с"
    < 3600   → "1м 5с"   (минуты + секунды, секунды без zero-pad)
    >= 3600  → "1ч 2м"   (часы + минуты, секунды отбрасываются)

    Отрицательное трактуется как 0; дробные секунды усекаются к int.
    """
    return " ".join(f"{n}{unit}" for n, unit in _duration_parts(seconds))


def format_compact_int(n: int) -> str:
    """Компактное целое: 999 → "999", 1000 → "1.0k", 12345 → "12.3k"."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)


def format_round_int(n: int) -> str:
    """Круглое целое для меток ленты: 999 → "999", 128300 → "128k",
    1234567 → "1.2M". Тысячи — целыми, с отбрасыванием остатка: 999999 —
    это "999k", а не "1000k"."""
    if n >= 1_000_000:
        return f"{n / 1_000_000:.1f}M"
    if n >= 1000:
        return f"{n // 1000}k"
    return str(n)
