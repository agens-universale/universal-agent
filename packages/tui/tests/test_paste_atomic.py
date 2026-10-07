"""Pilot-тесты: плашка ведёт себя как один символ (delete/del/стрелки)."""

import pytest

BIG5 = "a\n" * 4 + "a"
LABEL5 = "[Вставка #1 · 5 строк]"


async def _input(pilot):
    from gc_tui.widgets.command_input import CommandTextArea

    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


@pytest.mark.asyncio
async def test_backspace_deletes_whole_chip():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)  # курсор встаёт после плашки
        await pilot.pause()
        inp.action_delete_left()
        await pilot.pause()
        assert inp.text == ""
        assert inp._chips.get(1) == BIG5  # запись жива: Ctrl+Z вернёт плашку


@pytest.mark.asyncio
async def test_delete_right_before_chip_removes_it():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        inp.move_cursor((0, 0))  # перед плашкой
        inp.action_delete_right()
        await pilot.pause()
        assert inp.text == ""
        assert inp._chips.get(1) == BIG5  # запись жива: Ctrl+Z вернёт плашку


@pytest.mark.asyncio
async def test_chip_surrounded_by_text_atomic_delete():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.text = "AB"
        inp.move_cursor((0, 1))  # между A и B
        inp._handle_paste(BIG5)  # "A<label>B", курсор после label
        await pilot.pause()
        assert inp.text == f"A{LABEL5}B"
        inp.action_delete_left()  # backspace у правой границы плашки
        await pilot.pause()
        assert inp.text == "AB"
        assert inp._chips.get(1) == BIG5  # запись жива: Ctrl+Z вернёт плашку


@pytest.mark.asyncio
async def test_cursor_left_jumps_over_chip():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)  # курсор после плашки: (0, len(label))
        await pilot.pause()
        inp.action_cursor_left()
        await pilot.pause()
        assert inp.cursor_location == (0, 0)  # прыжок к левой границе


@pytest.mark.asyncio
async def test_cursor_right_jumps_over_chip():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        inp.move_cursor((0, 0))
        inp.action_cursor_right()
        await pilot.pause()
        assert inp.cursor_location == (0, len(LABEL5))


@pytest.mark.asyncio
async def test_numbering_stable_after_middle_chip_delete():
    """Удаление средней плашки не перенумеровывает соседей: #1 и #3 остаются."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services.paste_chips import CHIP_TEXT, token_spans

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste("a\n" * 4 + "a")  # плашка #1
        await pilot.pause()
        inp._handle_paste("b\n" * 4 + "b")  # плашка #2
        await pilot.pause()
        inp._handle_paste("c\n" * 4 + "c")  # плашка #3
        await pilot.pause()
        assert "[Вставка #1 · 5 строк]" in inp.text
        assert "[Вставка #2 · 5 строк]" in inp.text
        assert "[Вставка #3 · 5 строк]" in inp.text

        # удалить среднюю (#2): курсор на её правую границу + backspace
        span2 = next(
            s
            for s in token_spans(inp.text, inp._chips)
            if s[0] == (CHIP_TEXT, 2)
        )
        inp.move_cursor((span2[1], span2[3]))
        inp.action_delete_left()
        await pilot.pause()

        # соседи сохраняют свои номера, #2 удалена
        assert "[Вставка #1 · 5 строк]" in inp.text
        assert "[Вставка #2 · 5 строк]" not in inp.text
        assert "[Вставка #3 · 5 строк]" in inp.text
        assert (
            inp._chips.get(2) == "b\n" * 4 + "b"
        )  # запись жива: Ctrl+Z вернёт плашку
        assert inp._chips.get(1) is not None
        assert inp._chips.get(3) is not None
