"""Совместимость с Textual: патч выделения и протяжка ниже ленты.

Единственное место обращения к приватному API Textual. Всё — только для
версии `PATCHED_TEXTUAL`: на иной патч не ставится, протяжка ниже ленты
не работает, в журнал уходит предупреждение, страж
`test_textual_compat.py` падает с напоминанием пересмотреть модуль.
Исключение — `mounted` и `paint_after_mount`: только публичный API, к
версии не привязаны.

Видимость и геометрия Textual 8.2.8: перерисовка виджета за экраном или
чтение его `size` / `region` после прокрутки пересобирает полную карту
экрана (`Compositor.full_map`). `is_visible` смотрит готовую карту
видимых последнего кадра; ширины по раскладке (`laid_out_width`) — из
размера, который раскладка записала виджету, если он в последней
раскладке, — без поиска в карте.

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
версии Textual — обычный якорь. Прямая запись позиции ползунка также
обходит сброс кеша рисунка: при якорной раскладке сбрасываем его явно.

Утечка Textual 8.2.8: `StylesCache.get_inner_outer` — `lru_cache(1024)`
на методе экземпляра, и в ключ попадает `self`. Общий для класса кеш
держит кеш строк (`Strip` и их кеши) последних 1024 отрисованных
виджетов и после их снятия: окно ленты снимает ходы, а их отрисовка
остаётся в памяти. Патч кеширует ответ по одним цветам. Стенд —
`test_textual_compat.py`.

Удержание первой видимой строки (спека шага 2 окна ленты, §5.3): кадр
Textual 8.2.8 рисуется раньше сигнала раскладки, `call_after_refresh` —
тоже после кадра, поэтому сдвиг «после раскладки» даёт прыжок на кадр.
Компоновщик читает `scroll_offset` контейнера после его `arrange`
(_compositor.py:601 против 623/631): `AnchoredScroll.arrange` сдвигает
прокрутку на смещение удерживаемого виджета в новой раскладке и пишет её
через `set_reactive`, как якорь низа. Только для `PATCHED_TEXTUAL`.
"""

from __future__ import annotations

import asyncio
import logging
from bisect import bisect_right
from collections.abc import Callable
from functools import cache, lru_cache

import textual
from textual import selection
from textual.color import Color
from textual.containers import VerticalScroll
from textual.dom import NoScreen
from textual.geometry import Offset, Size
from textual.screen import Screen
from textual.style import Style
from textual.widget import AwaitMount, Widget
from textual.widgets import Static

PATCHED_TEXTUAL = "8.2.8"

# Потолок скорости протяжки ниже ленты (доля скорости Textual).
_MAX_BELOW_SPEED = 2.0
# Сколько экран ждёт монтирования строки, прежде чем рисовать без неё:
# страховка от строки, снятой до монтирования, — экран не замирает.
_PAINT_HOLD_LIMIT = 1.0
# Ждущие монтирования задачи — ссылка, чтобы их не собрал сборщик.
_PAINT_HOLDS: set[asyncio.Task] = set()

_log = logging.getLogger(__name__)


@cache
def textual_is_patched_version() -> bool:
    """Установлена ли версия Textual, под которую написан патч. Ответ
    запоминается: модуль Textual на каждое обращение к ``__version__``
    разбирает METADATA пакета, а проверка зовётся на каждую строку ленты.
    Подмена версии (тесты) — со сбросом ``cache_clear()``."""
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


@lru_cache(1024)
def _inner_outer(base_background: Color, background: Color):
    """Фоны внутри и снаружи рамки — как у Textual, ключ только цвета."""
    return (
        Style(background=base_background + background),
        Style(background=base_background),
    )


def _get_inner_outer(_styles_cache, base_background, background):
    return _inner_outer(base_background, background)


def apply_textual_patches() -> bool:
    """Поставить патчи знака `SelectStart` и кеша фонов `StylesCache`;
    идемпотентно.

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
    # Приватный модуль — после стража версии: на иной его может не быть.
    from textual._styles_cache import StylesCache

    selection.SelectStart.pointer_start_offset = _PATCHED_PROPERTY
    StylesCache.get_inner_outer = _get_inner_outer
    return True


class AnchoredScroll(VerticalScroll):
    """Прокрутка с якорем Textual и поправкой двух его ошибок 8.2.8:
    якорная прокрутка считается от высоты окна прошлой раскладки (лента
    сменила высоту — низ уезжает на разницу до следующего кадра) и ставится
    мимо валидатора (контент ниже окна — прокрутка в минус). Здесь — от
    высоты той же раскладки, не ниже нуля. Только для `PATCHED_TEXTUAL`:
    на иной версии — обычный якорь Textual."""

    _anchor_y = 0
    # Удерживаемый виджет и последняя полная раскладка (её размещения —
    # прежние места при следующей).
    _held: Widget | None = None
    _arranged = None
    # Ждут следующей полной раскладки (`call_after_layout`).
    _after_layout: tuple[Callable[[], None], ...] = ()

    def arrange(self, size: Size, optimal: bool = False):
        result = super().arrange(size, optimal)
        if not optimal and textual_is_patched_version():
            bottom = result.spatial_map.total_region.bottom
            self._anchor_y = max(0, bottom - size.height)
            previous, self._arranged = self._arranged, result
            if previous is not None and previous is not result:
                self._keep_held_row(previous, result, size)
            pending, self._after_layout = self._after_layout, ()
            for callback in pending:
                self.call_later(callback)
        return result

    def hold_first_visible(self, widget: Widget | None) -> None:
        """Пока лента не прилипла к низу, каждая новая раскладка держит
        ``widget`` на той же экранной строке: прокрутка сдвигается на его
        смещение (домонтирование сверху, смена ширины). ``None`` — не
        держать."""
        self._held = widget

    def call_after_layout(self, callback: Callable[[], None]) -> None:
        """``callback`` — после следующей полной раскладки ленты: решения
        по её высоте (под чужим экраном раскладки нет — ждёт). Иная версия
        Textual — после кадра."""
        if not textual_is_patched_version():
            self.call_after_refresh(callback)
            return
        self._after_layout = (*self._after_layout, callback)

    def first_visible(self) -> Widget | None:
        """Первый виджет, видимый в окне ленты, по последней раскладке —
        без пересборки карты экрана. Иная версия Textual или раскладки
        ещё не было — ``None``."""
        arranged = self._arranged
        if arranged is None or not textual_is_patched_version():
            return None
        placements = arranged.placements
        at = bisect_right(
            placements, self.scroll_y, key=lambda p: p.region.bottom
        )
        return placements[at].widget if at < len(placements) else None

    def placed_top(self, widget: Widget) -> int | None:
        """Верх ``widget`` в ленте по последней раскладке; нет в ней (или
        иная версия Textual) — ``None``."""
        arranged = self._arranged
        if arranged is None or not textual_is_patched_version():
            return None
        return _placed_y(arranged, widget)

    def placed_between(self, top: float, bottom: float) -> set[Widget] | None:
        """Дети ленты, задевающие строки ``[top, bottom)`` по последней
        раскладке, — без пересборки карты экрана. Раскладки не было (или
        иная версия Textual) — ``None``."""
        arranged = self._arranged
        if arranged is None or not textual_is_patched_version():
            return None
        placements = arranged.placements
        at = bisect_right(placements, top, key=lambda p: p.region.bottom)
        found: set[Widget] = set()
        for placement in placements[at:]:
            if placement.region.y >= bottom:
                break
            found.add(placement.widget)
        return found

    def _keep_held_row(self, previous, result, size: Size) -> None:
        held = self._held
        if held is None or self._anchor_held():
            return
        old, new = _placed_y(previous, held), _placed_y(result, held)
        if old is None or new is None or old == new:
            return
        bottom = result.spatial_map.total_region.bottom
        top = min(self.scroll_y + new - old, bottom - size.height)
        top = max(0, top)
        super().set_reactive(Widget.scroll_y, top)
        super().set_reactive(Widget.scroll_target_y, top)
        # Наблюдатель прокрутки обойдён: ползунок — явно.
        self.vertical_scrollbar.position = top

    def set_reactive(self, reactive, value) -> None:
        scroll = reactive in (Widget.scroll_y, Widget.scroll_target_y)
        # Версия — до приватного состояния якоря: на иной его может не быть.
        if scroll and textual_is_patched_version() and self._anchor_held():
            value = self._anchor_y
            if reactive is Widget.scroll_y:
                # Compositor пишет position ползунка напрямую, мимо reactive:
                # даже при верной позиции кеш может хранить прежний рисунок.
                self.vertical_scrollbar.refresh()
        super().set_reactive(reactive, value)

    def _anchor_held(self) -> bool:
        return self._anchored and not self._anchor_released


def _placed_y(arranged, widget: Widget) -> int | None:
    """Верх ``widget`` в раскладке ``arranged``; нет в ней — ``None``."""
    for placement in arranged.placements:
        if placement.widget is widget:
            return placement.region.y
    return None


async def mounted(parent: Widget, widgets: list[Widget]) -> None:
    """Дождаться монтирования детей, уже отданных `parent.mount()`."""
    await AwaitMount(parent, widgets)


def paint_after_mount(*widgets: Widget) -> asyncio.Task | None:
    """Экран не рисует, пока ``widgets``, уже отданные `mount()` одному
    родителю, не смонтированы (их дети собираются следующим тактом):
    перемены соседей, сделанные до того, ложатся одним кадром с ними. Уже
    смонтированные или без родителя — не ждутся. Задача отпускания —
    чтобы продолжить после монтажа; ждать нечего — ``None``."""
    pending = [
        w for w in widgets if not w.is_mounted and isinstance(w.parent, Widget)
    ]
    if not pending:
        return None
    parent = pending[0].parent
    batch = pending[0].app.batch_update()

    async def release() -> None:
        try:
            await asyncio.wait_for(mounted(parent, pending), _PAINT_HOLD_LIMIT)
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
    return task


def is_visible(widget: Widget | None) -> bool:
    """Был ли виджет в карте видимых последнего кадра — без пересборки
    карты (публичный `is_on_screen` идёт через поиск в полной карте и
    пересобирает её, а «видим» не отвечает). `None`, не смонтирован, без
    экрана — нет. Только для `PATCHED_TEXTUAL`: на иной версии или без
    поля компоновщика — «видим», как будто проверки нет."""
    if widget is None or not widget.is_mounted:
        return False
    try:
        screen = widget.screen
    except NoScreen:
        return False
    if not textual_is_patched_version():
        return True
    try:
        return widget in screen._compositor.visible_widgets
    except AttributeError:
        return True


def laid_out_width(widget: Widget) -> int:
    """Ширина контента (без отступов и рамки) по последней раскладке —
    то же, что `widget.size.width`, но без поиска в карте экрана (он у
    невидимого после прокрутки пересобирает карту). Не в последней полной
    раскладке (ещё не раскладывался, скрыт `display: none`, без экрана) —
    0, как у `size`: размер, записанный виджету раньше, у скрытого
    устаревает — полная раскладка его не обновляет, и после смены ширины
    терминала он врёт. Признак — карта последней полной раскладки
    компоновщика как есть (прокрутка её не меняет), без пересборки.
    Только для `PATCHED_TEXTUAL`: на иной версии или без поля —
    `widget.size.width`."""
    if not textual_is_patched_version():
        return widget.size.width
    try:
        laid = widget in widget.screen._compositor._full_map
    except (NoScreen, AttributeError):
        return widget.size.width
    if not laid:
        return 0
    return max(0, widget.outer_size.width - widget.styles.gutter.width)


def laid_out_scrollable_width(widget: Widget) -> int:
    """Ширина прокручиваемого контента (ещё и без полосы прокрутки) по
    последней раскладке — то же, что
    `widget.scrollable_content_region.width`, без поиска в карте."""
    return max(0, laid_out_width(widget) - widget.scrollbar_size_vertical)


def static_markup(widget: Static) -> bool:
    """Разбирает ли `widget` разметку в строковом содержимом (флаг
    `markup` конструктора, приватное поле Textual). Только для
    `PATCHED_TEXTUAL`: на иной версии или без поля — «разбирает»."""
    if not textual_is_patched_version():
        return True
    return bool(getattr(widget, "_render_markup", True))


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


def hook_auto_scroll(
    screen: Screen, container: Callable[[], Widget | None]
) -> bool:
    """Поставить `check_auto_scroll` хуком `_check_auto_scroll` экрана;
    `container` — лента на момент движения мыши.

    Только для `PATCHED_TEXTUAL`: на иной версии хук не ставится, экран
    остаётся с автопрокруткой Textual как есть — смена её сигнатуры или
    пропажа метода выделение не ломает. `True` — хук поставлен."""
    if not textual_is_patched_version():
        return False

    def check(select_widget, mouse_coordinate, delta_y) -> None:
        check_auto_scroll(
            screen, container(), select_widget, mouse_coordinate, delta_y
        )

    screen._check_auto_scroll = check
    return True
