"""Полотно, собираемое по мере прокрутки (Р23): места, окно, отпускание.

Механизм общий для вьюера правок и подтверждения отката, и проверяется он
здесь один раз — на голом полотне, без диффов: экранные стражи (`test_diff_*`)
смотрят на поведение, а этот модуль на контракт.

Главное, что здесь сторожится, — ЧЕСТНОСТЬ МЕСТА: место под участок
резервируется до того, как участок нарисован, поэтому полоса прокрутки не врёт
и содержимое не прыгает под курсором, когда участок наконец собрался.
"""
from __future__ import annotations

import pytest

pytest.importorskip("textual")

from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static

from gc_tui.widgets.lazy_body import (
    KEEP_FACTOR,
    OVERSCAN_SCREENS,
    BodySlot,
    LazyBody,
)

PART_LINES = 20
PARTS = 60
VIEW = (60, 20)


class _Host(App):
    """Хост с полотном во весь экран — без стилей приложения: их тут не нужно."""

    CSS = "LazyBody { height: 1fr; width: 1fr; }"

    def compose(self) -> ComposeResult:
        yield LazyBody(id="body")


def _text(number: int) -> Text:
    return Text("\n".join(f"часть {number} строка {line}"
                          for line in range(PART_LINES)))


def _slots(count: int = PARTS) -> list[BodySlot]:
    return [
        BodySlot(Static("", classes="chunk"), PART_LINES,
                 (lambda number=number: _text(number)))
        for number in range(count)
    ]


def _built(body: LazyBody) -> list[int]:
    """Номера собранных участков — по содержимому виджета, а не по внутренностям."""
    return [index for index, slot in enumerate(body._slots)
            if str(slot.widget.render())]


@pytest.mark.asyncio
async def test_places_are_reserved_before_the_parts_are_built():
    """Полотно знает свою высоту с первого кадра — до всякой отрисовки.

    Полоса прокрутки считается от неё: собери мы сначала одно окно, а высоту
    доводи по мере сборки — ползунок скакал бы на каждой прокрутке, а
    `scroll_visible` уводил бы кадр не туда.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        body.set_slots(_slots())
        await pilot.pause()

        assert body.virtual_size.height == PARTS * PART_LINES
        assert len(body.children) == PARTS          # места на все участки
        built = _built(body)
        assert built                                # кадр наполнен сразу
        assert len(built) < PARTS                   # но собрано не всё


@pytest.mark.asyncio
async def test_reserve_is_generous_enough_to_stay_ahead_of_pagedown():
    """Запас — с избытком: `PgDn` уводит кадр на высоту экрана за нажатие.

    Собранное обязано уходить вниз минимум на несколько таких шагов, иначе при
    зажатой клавише глаз догоняет сборку и видит пустое место.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        body.set_slots(_slots())
        await pilot.pause()

        viewport = body.scrollable_content_region.height
        built = _built(body)
        depth = (max(built) + 1) * PART_LINES - viewport
        assert depth >= viewport * OVERSCAN_SCREENS


@pytest.mark.asyncio
async def test_scrolling_builds_the_next_parts_and_releases_the_far_ones():
    """Прокрутка добирает следующие участки и отпускает уехавшие далеко."""
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        body.set_slots(_slots())
        await pilot.pause()
        first = set(_built(body))

        body.scroll_to(y=PARTS * PART_LINES // 2, animate=False)
        await pilot.pause()
        middle = set(_built(body))

        assert middle - first                       # добрали следующие
        assert first - middle                       # и отпустили далёкие
        assert not middle & {0, 1}                  # начало полотна отпущено


@pytest.mark.asyncio
async def test_building_moves_nothing_on_the_canvas():
    """Собранный участок занимает ровно своё место — ни строкой больше.

    Иначе содержимое ехало бы под глазом на каждой прокрутке: место под текстом
    и сам текст обязаны совпадать до строки.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        body.set_slots(_slots())
        await pilot.pause()
        before = [slot.widget.virtual_region for slot in body._slots]
        height = body.virtual_size.height

        body.scroll_to(y=PARTS * PART_LINES // 2, animate=False)
        await pilot.pause()

        assert body.virtual_size.height == height
        assert [slot.widget.virtual_region for slot in body._slots] == before
        # Собранный участок и правда высотой в свои строки.
        built = _built(body)[0]
        rendered = str(body._slots[built].widget.render()).splitlines()
        assert len(rendered) == PART_LINES


@pytest.mark.asyncio
async def test_ensure_builds_the_part_before_the_frame_goes_there():
    """Прыжок курсора: участок собирается ДО того, как кадр к нему поедет.

    Прокрутка собрала бы его лишь следующим тактом — и прыжок показал бы пустое
    место там, где обещана правка.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        slots = _slots()
        body.set_slots(slots)
        await pilot.pause()
        assert PARTS - 1 not in _built(body)        # хвост полотна далеко

        body.ensure(slots[-1].widget)               # без прокрутки

        assert PARTS - 1 in _built(body)


@pytest.mark.asyncio
async def test_service_lines_are_never_emptied():
    """Служебная строка живёт наполненной всегда — она дёшева и держит высоту.

    Её высота зависит от ширины окна и переноса, и угадывать эту высоту полотну
    нечем: пустое место под перенёсшимся предупреждением разъехалось бы с
    содержимым.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        head = Static("заголовок файла")
        body.set_slots([BodySlot(head)] + _slots())
        await pilot.pause()

        body.scroll_end(animate=False)
        await pilot.pause()

        from textual.css.scalar import Unit

        assert str(head.render()) == "заголовок файла"
        assert head.styles.height.unit is Unit.AUTO  # высотой распоряжается сам
        assert body._slots[1].widget.styles.height.unit is Unit.CELLS


@pytest.mark.asyncio
async def test_nothing_is_released_while_a_selection_is_alive():
    """Пока живо выделение, содержимое не отпускается.

    Протяжка с автопрокруткой набирает виджеты, уехавшие из кадра; опустошённый
    участок вырезал бы из копии свой кусок текста — молча.
    """
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        slots = _slots()
        body.set_slots(slots)
        await pilot.pause()
        first = set(_built(body))

        from textual.widget import Selection
        app.screen.selections = {slots[0].widget: Selection(None, None)}
        body.scroll_end(animate=False)
        await pilot.pause()

        assert first <= set(_built(body))           # ничего не отпущено
        assert PARTS - 1 in _built(body)            # а хвост при этом собран


@pytest.mark.asyncio
async def test_keep_window_is_wider_than_the_build_window():
    """Гистерезис: участок на границе не мигает содержимым туда-обратно.

    Без него шаг колеса выталкивал бы участок из окна сборки и тут же
    возвращал — и каждый шаг стоил бы пересборки.
    """
    assert KEEP_FACTOR > 1
    app = _Host()
    async with app.run_test(size=VIEW) as pilot:
        body = app.query_one(LazyBody)
        body.set_slots(_slots())
        await pilot.pause()
        first = set(_built(body))

        body.scroll_to(y=PART_LINES, animate=False)   # шаг на один участок
        await pilot.pause()

        assert first <= set(_built(body))             # прежнее осталось собранным
