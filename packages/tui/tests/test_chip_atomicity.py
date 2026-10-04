"""Плашка в поле ввода — один символ для любого действия (смоук картинок 2026-09-28).

Два правила поля: курсор и края выделения никогда не стоят внутри плашки, а правка,
задевшая часть плашки, забирает её целиком. Проверяются оба вида плашек — картинки и
объёмной вставки — и все пути, которыми плашку раньше можно было разрезать: клик
мышью, стрелки по вертикали, работа со словами, выделение Shift и мышью.
"""

import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult

from gc_tui.widgets.command_input import CommandTextArea
from image_helpers import make_attachment

BIG5 = "a\n" * 4 + "a"
PREFIX, SUFFIX = "abc ", " def"


class _Host(App):
    def compose(self) -> ComposeResult:
        yield CommandTextArea(id="inp")


def _put_chip(
    inp: CommandTextArea,
    kind: str,
    *,
    prefix: str = PREFIX,
    suffix: str = SUFFIX,
) -> str:
    """Поле с одной плашкой вида `kind` между `prefix` и `suffix`; вернуть её подпись."""
    inp.load_text("")
    if kind == "image":
        label = inp._chips.add_image(make_attachment("a" * 16, 1), 1)
    else:
        _chip_id, label = inp._chips.add(BIG5)
    inp.insert(prefix + label + suffix)
    return label


@pytest.fixture(params=["image", "text"])
def kind(request):
    return request.param


async def _field(pilot) -> CommandTextArea:
    inp = pilot.app.query_one("#inp", CommandTextArea)
    inp.focus()
    await pilot.pause()
    return inp


@pytest.mark.asyncio
async def test_a_click_inside_a_chip_puts_the_cursor_on_its_border(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        await pilot.pause()

        await pilot.click(
            inp, offset=(len(PREFIX) + 3, 0)
        )  # ближе к левому краю
        await pilot.pause()

        assert inp.cursor_location == (0, len(PREFIX))
        await pilot.press("x")
        assert (
            inp.text == PREFIX + "x " + label + SUFFIX
        )  # плашка цела, пробел — сам


@pytest.mark.parametrize("key", ["alt+backspace", "ctrl+w", "ctrl+backspace"])
@pytest.mark.asyncio
async def test_deleting_a_word_left_takes_the_whole_chip(kind, key):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        inp.move_cursor((0, len(PREFIX) + len(label)))

        await pilot.press(key)
        await pilot.pause()

        assert inp.text == PREFIX + SUFFIX
        inp.action_undo()
        await pilot.pause()
        assert inp.text == PREFIX + label + SUFFIX  # Ctrl+Z вернул целиком


@pytest.mark.asyncio
async def test_deleting_a_word_right_takes_the_whole_chip(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        _put_chip(inp, kind)
        inp.move_cursor((0, len(PREFIX)))

        await pilot.press("alt+delete")
        await pilot.pause()

        assert inp.text == PREFIX + SUFFIX


@pytest.mark.asyncio
async def test_word_moves_step_over_a_chip(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        start, end = len(PREFIX), len(PREFIX) + len(label)

        inp.move_cursor((0, end))
        await pilot.press("ctrl+left")
        assert inp.cursor_location == (0, start)

        await pilot.press("ctrl+right")
        assert inp.cursor_location == (0, end)


@pytest.mark.asyncio
async def test_shift_selection_takes_the_whole_chip_and_can_shrink_back(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        start, end = len(PREFIX), len(PREFIX) + len(label)
        inp.move_cursor((0, end))

        await pilot.press("shift+left")
        assert sorted(inp.selection) == [(0, start), (0, end)]
        await pilot.press("shift+right")  # назад — выделение пусто
        assert inp.selection.is_empty and inp.cursor_location == (0, end)

        await pilot.press("shift+left")
        await pilot.press("backspace")
        await pilot.pause()
        assert inp.text == PREFIX + SUFFIX


@pytest.mark.parametrize("key", ["down", "up"])
@pytest.mark.asyncio
async def test_vertical_moves_do_not_land_inside_a_chip(kind, key):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        other = "0123456789"
        if key == "down":
            label = _put_chip(inp, kind, prefix=other + "\n", suffix=" x")
            inp.move_cursor((0, 3))
            chip_row = 1
        else:
            label = _put_chip(inp, kind, prefix="", suffix="\n" + other)
            inp.move_cursor((1, 3))
            chip_row = 0

        await pilot.press(key)

        assert inp.cursor_location == (chip_row, 0)  # ближняя граница
        assert label in inp.text


@pytest.mark.asyncio
async def test_a_mouse_drag_ending_inside_a_chip_takes_the_whole_chip(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        await pilot.pause()

        await pilot.mouse_down(inp, offset=(1, 0))
        await pilot.hover(inp, offset=(len(PREFIX) + 3, 0))
        await pilot.mouse_up(inp, offset=(len(PREFIX) + 3, 0))
        await pilot.pause()

        low, high = sorted(inp.selection)
        assert low[1] < len(PREFIX)  # начало протяжки — до плашки
        assert high == (
            0,
            len(PREFIX) + len(label),
        )  # конец — за плашкой целиком


@pytest.mark.asyncio
async def test_a_mouse_drag_within_a_chip_keeps_it_whole(kind):
    """Протяжка внутри одной плашки: выделение — вся плашка, сколько бы раз мышь ни
    сдвинулась."""
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind)
        await pilot.pause()

        await pilot.mouse_down(inp, offset=(len(PREFIX) + 3, 0))
        await pilot.hover(inp, offset=(len(PREFIX) + 5, 0))
        await pilot.hover(inp, offset=(len(PREFIX) + 7, 0))
        await pilot.mouse_up(inp, offset=(len(PREFIX) + 7, 0))
        await pilot.pause()

        assert sorted(inp.selection) == [
            (0, len(PREFIX)),
            (0, len(PREFIX) + len(label)),
        ]


# --- пробел у плашки — по требованию ------------------------------------------


@pytest.mark.asyncio
async def test_typing_right_after_a_chip_adds_a_space(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind, prefix="", suffix="")
        inp.move_cursor((0, len(label)))

        await pilot.press("h")
        assert inp.text == label + " h"
        inp.action_undo()
        await pilot.pause()
        assert inp.text == label  # пробел ушёл вместе с буквой

        await pilot.press("h", "i")
        assert inp.text == label + " hi"


@pytest.mark.asyncio
async def test_typing_right_before_a_chip_adds_a_space_after_the_text(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind, prefix="", suffix="")
        inp.move_cursor((0, 0))

        await pilot.press("h", "i")

        assert inp.text == "hi " + label
        assert inp.cursor_location == (0, 2)  # курсор — до пробела


@pytest.mark.asyncio
async def test_a_paste_right_after_a_chip_adds_a_space(kind):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind, prefix="", suffix="")
        inp.move_cursor((0, len(label)))

        inp._handle_paste("глянь")
        await pilot.pause()

        assert inp.text == label + " глянь"


@pytest.mark.parametrize("key, expected", [("space", " "), ("ctrl+j", "\n")])
@pytest.mark.asyncio
async def test_whitespace_right_after_a_chip_gets_no_extra_space(
    kind, key, expected
):
    async with _Host().run_test(size=(80, 10)) as pilot:
        inp = await _field(pilot)
        label = _put_chip(inp, kind, prefix="", suffix="")
        inp.move_cursor((0, len(label)))

        await pilot.press(key)

        assert inp.text == label + expected
