"""Временные строки мыслей и записи треда (спека мыслей §7).

Запись мысли — истина; строка из стрима временная. Пришла запись
попытки — какие временные строки снять. Модуль — только решение, без
виджетов: строки передаются метками своих попыток в порядке появления.
"""

from __future__ import annotations

from typing import Sequence


def stale_on_record(
    provisional: Sequence[str | None],
    attempt: str | None,
    *,
    current: str | None = None,
) -> list[int]:
    """Индексы временных строк, которые снять, когда пришла запись попытки
    ``attempt``: все до совпавшей (совпавшая становится записанной, более
    новые попытки идут дальше); совпадения нет — все, кроме строки
    идущей попытки ``current`` (её запись ещё впереди)."""
    if attempt in provisional:
        return list(range(provisional.index(attempt)))
    return [i for i, a in enumerate(provisional) if a is None or a != current]
