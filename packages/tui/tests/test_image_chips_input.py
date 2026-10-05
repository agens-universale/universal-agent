"""Pilot-тесты: image-плашка в поле ввода — один символ; стёртую плашку (текстовую и image)
Ctrl+Z возвращает вместе с содержимым; сброс плашек — только программной очисткой поля
(отправка, двойной Esc)."""

import pytest

from gc_core.attachments.model import chip_text
from image_helpers import make_attachment

BIG5 = "a\n" * 4 + "a"
LABEL5 = "[Вставка #1 · 5 строк]"


async def _input(pilot):
    from gc_tui.widgets.command_input import CommandTextArea

    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


@pytest.mark.asyncio
async def test_image_chips_go_in_as_one_edit():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        a, b = make_attachment("a" * 16, 3), make_attachment("b" * 16, 4)
        inp.insert_image_chips([a, b], 3)
        await pilot.pause()
        assert inp.text == f"{chip_text(3)} {chip_text(4)}"
        assert inp.images_in_buffer() == (a, b)
        inp.action_undo()
        await pilot.pause()
        assert inp.text == ""


@pytest.mark.asyncio
async def test_undo_brings_back_a_deleted_image_chip_with_its_attachment():
    """Сообщение из одной картинки (спека §15): Backspace стёр плашку — буфер пуст, но это
    правка, а не отправка; Ctrl+Z вернул подпись, и она снова плашка с вложением."""
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        a = make_attachment("a" * 16, 1)
        inp.insert_image_chips([a], 1)
        await pilot.pause()
        inp.action_delete_left()
        await pilot.pause()
        assert inp.text == ""
        inp.action_undo()
        await pilot.pause()
        assert inp.text == chip_text(1)
        assert inp.images_in_buffer() == (a,)


@pytest.mark.asyncio
async def test_undo_brings_back_a_deleted_text_chip_with_its_content():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        inp.action_delete_left()
        await pilot.pause()
        assert inp.text == ""
        inp.action_undo()
        await pilot.pause()
        assert inp.text == LABEL5
        assert inp.submission_text() == BIG5


@pytest.mark.asyncio
async def test_undo_after_an_inline_expand_brings_the_chip_back():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp._handle_paste(BIG5)
        await pilot.pause()
        inp._handle_paste(BIG5)  # та же паста в окне — разворот инлайн
        await pilot.pause()
        assert inp.text == BIG5
        inp.action_undo()
        await pilot.pause()
        assert inp.text == LABEL5
        assert inp.submission_text() == BIG5


@pytest.mark.asyncio
async def test_an_image_chip_is_one_character_for_cursor_and_delete():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.text = "AB"
        inp.move_cursor((0, 1))
        inp.insert_image_chips([make_attachment("a" * 16, 1)], 1)
        await pilot.pause()
        assert inp.text == f"A {chip_text(1)} B"
        inp.move_cursor((0, 2 + len(chip_text(1))))  # правая граница плашки
        inp.action_cursor_left()  # прыжок через плашку к левой границе
        assert inp.cursor_location == (0, 2)
        inp.action_delete_right()  # Del у левой границы — плашка целиком
        await pilot.pause()
        assert inp.text == "A  B"


@pytest.mark.parametrize(
    "before, cursor, after",
    [
        ("", (0, 0), "{chip}"),
        ("почему", (0, 6), "почему {chip}"),
        ("почему ", (0, 7), "почему {chip}"),
        ("почему", (0, 0), "{chip} почему"),
        (" почему", (0, 0), "{chip} почему"),
        ("а\nб", (1, 0), "а\n{chip} б"),
    ],
)
@pytest.mark.asyncio
async def test_a_chip_is_separated_from_neighbouring_text_by_a_space(
    before, cursor, after
):
    """Плашка не слипается с текстом (визуальный контракт §2): пробел — только там, где
    сосед не пробельный; в начале и в конце поля — без пробела (набранное следом получит
    пробел само, `test_chip_atomicity`)."""
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.text = before
        inp.move_cursor(cursor)
        inp.insert_image_chips([make_attachment("a" * 16, 1)], 1)
        await pilot.pause()
        assert inp.text == after.format(chip=chip_text(1))


@pytest.mark.asyncio
async def test_the_same_image_pasted_again_reuses_its_chip():
    """Та же картинка второй раз — её прежняя плашка, номер не тратится (без мёртвых #N)."""
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        a, b = make_attachment("a" * 16, 0), make_attachment("b" * 16, 0)
        assert inp.insert_image_chips([a], 1) == 1
        assert inp.insert_image_chips([make_attachment("a" * 16, 0)], 2) == 0
        assert inp.insert_image_chips([b], 2) == 1
        await pilot.pause()
        assert inp.text == f"{chip_text(1)} {chip_text(1)} {chip_text(2)}"
        assert [x.id for x in inp.images_in_buffer()] == [a.id, b.id]


@pytest.mark.asyncio
async def test_programmatic_clear_drops_chips_user_deletion_keeps_them():
    """Опустевший от правки буфер плашки хранит; программная очистка (отправка, двойной
    Esc — `inp.text = ""`) сбрасывает: TextArea.load_text стирает историю undo."""
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        a = make_attachment("a" * 16, 1)
        inp.insert_image_chips([a], 1)
        await pilot.pause()
        inp.select_all()
        inp.action_delete_left()  # человек стёр всё
        await pilot.pause()
        assert inp.text == ""
        assert inp._chips.images_in_text(chip_text(1)) == [a]
        inp.text = ""  # отправка / двойной Esc
        assert inp._chips.images_in_text(chip_text(1)) == []
