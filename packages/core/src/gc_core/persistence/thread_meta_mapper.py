"""Чтение числовых полей состояния треда."""

from __future__ import annotations

from typing import Any, Optional


def read_optional_nonneg_int(value: Any) -> Optional[int]:
    """Число есть — неотрицательный int. null, отсутствие и не-число — None.

    Не ``int(value or 0)``: ноль и «числа нет» здесь разные.
    """
    if value is None or isinstance(value, bool) or not isinstance(value, int):
        return None
    if value < 0:
        return None
    return value
