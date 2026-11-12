"""Счётчик раскладок экрана Textual по вызывающему — наблюдатель для тестов.

`Compositor._arrange_root` раскладывает экран целиком; зовут его полный
`reflow`, быстрый путь прокрутки `reflow_visible` и пересборка полной карты
виджетов `full_map`. Законные `reflow` отличаются от дорогих `full_map`
только вызывающим, поэтому счёт разнесён по нему."""

from __future__ import annotations

import sys
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Iterator

from textual._compositor import Compositor


@dataclass
class ArrangeCounts:
    """Раскладки экрана за время `count_arranges()` по вызывающему."""

    reflow: int = 0
    reflow_visible: int = 0
    full_map: int = 0


@contextmanager
def count_arranges() -> Iterator[ArrangeCounts]:
    """Считать `Compositor._arrange_root` по вызывающему, пока открыт блок."""
    counts = ArrangeCounts()
    original = Compositor._arrange_root

    def counting(self, *args, **kwargs):
        caller = sys._getframe(1).f_code.co_name
        if hasattr(counts, caller):
            setattr(counts, caller, getattr(counts, caller) + 1)
        return original(self, *args, **kwargs)

    Compositor._arrange_root = counting
    try:
        yield counts
    finally:
        Compositor._arrange_root = original
