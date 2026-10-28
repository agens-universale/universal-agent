"""Совместимость с Textual: патч выделения и протяжка ниже ленты.

Единственное место обращения к приватному API Textual. Всё — только для
версии `PATCHED_TEXTUAL`: на иной патч не ставится, протяжка ниже ленты
не работает, в журнал уходит предупреждение, страж
`test_textual_compat.py` падает с напоминанием пересмотреть модуль.
Исключение — `mounted` и `paint_after_mount`: только публичный API, к
версии не привязаны.

Ошибка Textual 8.2.8 (апстрим #6639): `SelectStart.pointer_start_offset`
прибавляет сдвиг прокрутки контейнера вместо вычитания — при прокрутке во
время выделения его начало уезжает. Стенды — `test_textual_compat.py`
(перенос `repro.py` и `repro2.py` из решений захода TUI 2026-10-20).

Ошибки якоря Textual 8.2.8: раскладка ставит якорному контейнеру
`scroll_y` = низ контента − высота окна через `set_reactive`, мимо
валидатора, и высоту берёт из прошлой раскладки (`container_size`
обновляется после). Лента сменила высоту (бар, баннер, список у ввода) —
низ уезжает на разницу до следующего кадра; контент ниже окна — прокрутка
уходит в минус, контент прижат к низу окна. `AnchoredScroll` считает
якорную прокрутку от высоты той же раскладки, не ниже нуля; на иной
версии Textual — обычный якорь.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable

import textual
from textual import selection
from textual.containers import VerticalScroll
from textual.geometry import Offset, Size
from textual.screen import Screen
from textual.widget import AwaitMount, Widget

PATCHED_TEXTUAL = "8.2.8"

# Потолок скорости протяжки ниже ленты (доля скорости Textual).
_MAX_BELOW_SPEED = 2.0
# Сколько экран ждёт монтирования строки, прежде чем рисовать без неё:
# страховка от строки, снятой до монтирования, — экран не замирает.
_PAINT_HOLD_LIMIT = 1.0
# Ждущие монтирования задачи — ссылка, чтобы их не собрал сборщик.
_PAINT_HOLDS: set[asyncio.Task] = set()

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


class AnchoredScroll(VerticalScroll):
    """Прокрутка с якорем Textual и поправкой двух его ошибок 8.2.8:
    якорная прокрутка считается от высоты окна прошлой раскладки (лента
    сменила высоту — низ уезжает на разницу до следующего кадра) и ставится
    мимо валидатора (контент ниже окна — прокрутка в минус). Здесь — от
    высоты той же раскладки, не ниже нуля. Только для `PATCHED_TEXTUAL`:
    на иной версии — обычный якорь Textual."""

    _anchor_y = 0

    def arrange(self, size: Size, optimal: bool = False):
        result = super().arrange(size, optimal)
        if not optimal and textual_is_patched_version():
            bottom = result.spatial_map.total_region.bottom
            self._anchor_y = max(0, bottom - size.height)
        return result

    def set_reactive(self, reactive, value) -> None:
        scroll = reactive in (Widget.scroll_y, Widget.scroll_target_y)
        # Версия — до приватного состояния якоря: на иной его может не быть.
        if scroll and textual_is_patched_version() and self._anchor_held():
            value = self._anchor_y
        super().set_reactive(reactive, value)

    def _anchor_held(self) -> bool:
        return self._anchored and not self._anchor_released


async def mounted(parent: Widget, widgets: list[Widget]) -> None:
    """Дождаться монтирования детей, уже отданных `parent.mount()`."""
    await AwaitMount(parent, widgets)


def paint_after_mount(widget: Widget) -> None:
    """Экран не рисует, пока ``widget``, уже отданный `mount()`, не
    смонтирован (его дети собираются следующим тактом): перемены соседей,
    сделанные до того, ложатся одним кадром с ним. Уже смонтированный или
    без родителя — ничего не ждёт."""
    parent = widget.parent
    if widget.is_mounted or not isinstance(parent, Widget):
        return
    batch = widget.app.batch_update()

    async def release() -> None:
        try:
            await asyncio.wait_for(
                mounted(parent, [widget]), _PAINT_HOLD_LIMIT
            )
        except TimeoutError:
            _log.debug("строка не смонтирована за %s с", _PAINT_HOLD_LIMIT)
        finally:
            batch.__exit__(None, None, None)

    # Сначала путь отпускания, потом удержание: задача стартует не раньше
    # следующего такта, а не заведённая задача экран не держит.
    task = asyncio.get_running_loop().create_task(release())
    batch.__enter__()
    _PAINT_HOLDS.add(task)
    task.add_done_callback(_PAINT_HOLDS.discard)


def is_being_removed(widget: Widget) -> bool:
    """Виджет снят `remove()`, но ещё в DOM до следующего такта: `query()`
    его находит."""
    return bool(getattr(widget, "_pruning", False))


def is_selecting(screen: Screen) -> bool:
    """Идёт ли выделение мышью на экране."""
    return bool(getattr(screen, "_selecting", False))


def watch_selecting(screen: Screen, callback: Callable[[bool], None]) -> bool:
    """Звать `callback` на начало и конец выделения мышью на экране.

    Только для `PATCHED_TEXTUAL`: на иной версии не подписывается
    (`False`)."""
    if not textual_is_patched_version():
        return False
    screen.watch(screen, "_selecting", callback)
    return True


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
    # Приватный модуль — после стража версии: на иной его может не быть.
    from textual._auto_scroll import get_auto_scroll_regions

    lines = screen.app.SELECT_AUTO_SCROLL_LINES
    _up, down = get_auto_scroll_regions(
        container.content_region, auto_scroll_lines=lines
    )
    speed = min((int(mouse_y) - down.y) / lines, _MAX_BELOW_SPEED)
    if speed <= 0:
        return False
    screen._start_auto_scroll(container, +1, speed)
    return True


def check_auto_scroll(
    screen: Screen,
    container: Widget | None,
    select_widget: Widget,
    mouse_coordinate: tuple[float, float],
    delta_y: float,
) -> None:
    """Хук `Screen._check_auto_scroll`: автопрокрутка выделения Textual,
    конец выделения — в `container` (`keep_select_end_in`), протяжка ниже
    него (`auto_scroll_below`). Правило Textual: чисто горизонтальное
    движение протяжку не запускает, но идущую не гасит — признак берётся
    до вызова Textual (над полем ввода тот прокрутку останавливает)."""
    was_running = auto_scroll_running(screen)
    Screen._check_auto_scroll(screen, select_widget, mouse_coordinate, delta_y)
    if container is None:
        return
    keep_select_end_in(screen, container, mouse_coordinate)
    if was_running or abs(delta_y) >= 1:
        auto_scroll_below(screen, container, mouse_coordinate[1])
