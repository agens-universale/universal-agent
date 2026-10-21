"""Совместимость с Textual: патч выделения и протяжка ниже ленты.

Единственное место обращения к приватному API Textual. Всё — только для
версии `PATCHED_TEXTUAL`: на иной патч не ставится, протяжка ниже ленты
не работает, в журнал уходит предупреждение, страж
`test_textual_compat.py` падает с напоминанием пересмотреть модуль.

Ошибка Textual 8.2.8 (апстрим #6639): `SelectStart.pointer_start_offset`
прибавляет сдвиг прокрутки контейнера вместо вычитания — при прокрутке во
время выделения его начало уезжает. Стенды — `test_textual_compat.py`
(перенос `repro.py` и `repro2.py` из решений захода TUI 2026-10-20).
"""

from __future__ import annotations

import logging

import textual
from textual import selection
from textual._auto_scroll import get_auto_scroll_regions
from textual.geometry import Offset
from textual.screen import Screen
from textual.widget import Widget

PATCHED_TEXTUAL = "8.2.8"

# Потолок скорости протяжки ниже ленты (доля скорости Textual).
_MAX_BELOW_SPEED = 2.0

_log = logging.getLogger(__name__)


def textual_is_patched_version() -> bool:
    """Установлена ли версия Textual, под которую написан патч."""
    return textual.__version__ == PATCHED_TEXTUAL


def _pointer_start_offset(self: selection.SelectStart) -> Offset:
    """Начало выделения с поправкой на прокрутку — со знаком минус."""
    scrolled = (
        self.container.scroll_offset - self.container_initial_scroll_offset
    )
    return (
        self.container.region.offset + self.container_pointer_delta - scrolled
    )


_PATCHED_PROPERTY = property(_pointer_start_offset)


def apply_textual_patches() -> bool:
    """Поставить патч знака `SelectStart`; идемпотентно.

    `False` — версия Textual не та, патч не ставится (предупреждение
    в журнал)."""
    if not textual_is_patched_version():
        _log.warning(
            "Textual %s вместо %s: патч выделения не поставлен, "
            "пересмотреть gc_tui.textual_compat",
            textual.__version__,
            PATCHED_TEXTUAL,
        )
        return False
    selection.SelectStart.pointer_start_offset = _PATCHED_PROPERTY
    return True


def is_selecting(screen: Screen) -> bool:
    """Идёт ли выделение мышью на экране."""
    return bool(getattr(screen, "_selecting", False))


def stop_selecting(screen: Screen) -> None:
    """Снять признак выделения (запас к `clear_selection`)."""
    screen._selecting = False


def auto_scroll_running(screen: Screen) -> bool:
    """Идёт ли автопрокрутка выделения (таймер Textual)."""
    return getattr(screen, "_auto_select_scroll_timer", None) is not None


def _inside(widget: Widget, container: Widget) -> bool:
    return container in widget.ancestors_with_self


def _content_widget_at(
    screen: Screen, container: Widget, x: int, y: int
) -> Widget | None:
    """Строка `container` в точке, под накладками; на интервале — ближайшая
    выше."""
    for row in range(y, container.content_region.y - 1, -1):
        for widget, _region in screen._compositor.get_widgets_at(x, row):
            if not widget.is_container and _inside(widget, container):
                return widget
    return None


def keep_select_end_in(
    screen: Screen, container: Widget, mouse_coordinate: tuple[float, float]
) -> bool:
    """Выделение, начатое в `container`, не выходит за него.

    Конец над чужим виджетом (накладка, поле ввода, футер) Textual
    выделяет общим предком — экраном, и тот, если начало уехало вверх
    прокруткой, берёт весь `container` с первой строки (и текст чужого
    виджета). Конец переносится на строку `container` под указателем,
    прижатым к его области, — она выделяется целиком. `True` — конец
    перенесён."""
    state = getattr(screen, "_select_state", None)
    if (
        not textual_is_patched_version()
        or state is None
        or state.end is None
        or _inside(state.end.container, container)
        or not _inside(state.start.container, container)
    ):
        return False
    region = container.content_region
    x, y = (int(v) for v in mouse_coordinate)
    x = min(max(x, region.x), region.right - 1)
    y = min(max(y, region.y), region.bottom - 1)
    widget = _content_widget_at(screen, container, x, y)
    if widget is None:
        return False
    screen._select_state = state.update_end(
        Offset(x, y), selection.SelectEnd(widget.parent, widget, None)
    )
    return True


def auto_scroll_below(
    screen: Screen, container: Widget, mouse_y: float
) -> bool:
    """Протяжка выделения у нижнего края `container` и ниже него.

    Textual ищет прокручиваемого предка виджета под указателем: над
    накладкой, полем ввода или футером его нет, и автопрокрутка стоит.
    Здесь — та же прокрутка вниз, если Textual свою не запустил, идёт
    выделение и указатель в нижней зоне `container` или ниже. Скорость
    растёт с расстоянием от зоны, как у Textual, с потолком."""
    if (
        not textual_is_patched_version()
        or auto_scroll_running(screen)
        or not is_selecting(screen)
        or container.scroll_y >= container.max_scroll_y
    ):
        return False
    lines = screen.app.SELECT_AUTO_SCROLL_LINES
    _up, down = get_auto_scroll_regions(
        container.content_region, auto_scroll_lines=lines
    )
    speed = min((int(mouse_y) - down.y) / lines, _MAX_BELOW_SPEED)
    if speed <= 0:
        return False
    screen._start_auto_scroll(container, +1, speed)
    return True
