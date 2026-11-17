"""Счётчик раскладок экрана Textual по вызывающему — наблюдатель для тестов.

`Compositor._arrange_root` раскладывает экран целиком; зовут его полный
`reflow`, быстрый путь прокрутки `reflow_visible` и пересборка полной карты
виджетов `full_map`. Законные `reflow` отличаются от дорогих `full_map`
только вызывающим, поэтому счёт разнесён по нему.

Счёт идёт по именам функций Textual: переименуй их апстрим — счётчики
молча останутся нулями. Поэтому при входе в блок сверяется, что каждый
считаемый вызывающий есть у `Compositor` и всё ещё зовёт `_arrange_root`."""

from __future__ import annotations

import inspect
import sys
from contextlib import contextmanager
from dataclasses import dataclass, fields
from typing import Iterator

from textual._compositor import Compositor


@dataclass
class ArrangeCounts:
    """Раскладки экрана за время `count_arranges()` по вызывающему."""

    reflow: int = 0
    reflow_visible: int = 0
    full_map: int = 0


def _check_callers() -> None:
    """Каждое поле `ArrangeCounts` — метод или свойство `Compositor`,
    зовущее `_arrange_root`; иначе — ошибка, а не нули."""
    for field in fields(ArrangeCounts):
        member = inspect.getattr_static(Compositor, field.name, None)
        code = getattr(getattr(member, "fget", member), "__code__", None)
        if code is None or "_arrange_root" not in code.co_names:
            raise RuntimeError(
                f"Compositor.{field.name} не зовёт _arrange_root: "
                "Textual изменился, пересмотреть arrange_counter"
            )


@contextmanager
def count_arranges() -> Iterator[ArrangeCounts]:
    """Считать `Compositor._arrange_root` по вызывающему, пока открыт блок."""
    _check_callers()
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
