"""Форматирование длительности и компактных чисел для отображения в TUI.

Чистая презентация: без зависимостей от контроллеров и доменных модулей.
"""
from __future__ import annotations


def format_duration(seconds: float) -> str:
    """Человекочитаемая длительность.

    < 60     → "45с"
    < 3600   → "1м 5с"   (минуты + секунды, секунды без zero-pad)
    >= 3600  → "1ч 2м"   (часы + минуты, секунды отбрасываются)

    Отрицательное трактуется как 0; дробные секунды усекаются к int.
    """
    total = int(seconds) if seconds > 0 else 0
    if total < 60:
        return f"{total}с"
    if total < 3600:
        return f"{total // 60}м {total % 60}с"
    return f"{total // 3600}ч {(total % 3600) // 60}м"


def format_compact_int(n: int) -> str:
    """Компактное целое: 999 → "999", 1000 → "1.0k", 12345 → "12.3k"."""
    if n >= 1000:
        return f"{n / 1000:.1f}k"
    return str(n)
