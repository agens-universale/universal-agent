"""Перенос строк в поле ввода не рвёт плашку (смоук картинок 2026-09-25).

Сторож механизма `gc_tui.widgets.chip_wrap`: он опирается на внутреннее устройство
Textual (`WrappedDocument`, навигатор курсора). Обновление Textual, которое сломает
механизм, покраснеет здесь.
"""

import pytest

from gc_core.attachments.model import chip_text
from gc_tui.widgets.chip_wrap import mask_chip_spaces
from image_helpers import make_attachment

BIG5 = "a\n" * 4 + "a"
LABEL5 = "[Вставка #1 · 5 строк]"


def test_spaces_inside_chips_are_masked_with_the_same_width():
    line = f"a {chip_text(1)} b"
    masked = mask_chip_spaces(line, [(2, 2 + len(chip_text(1)))])
    assert masked == "a [Изображение_#1] b"
    assert len(masked) == len(line)


async def _input(pilot):
    from gc_tui.widgets.command_input import CommandTextArea

    return pilot.app.screen.query_one("#cmdinput", CommandTextArea)


def _prefix_splitting(label: str, width: int) -> str:
    """Текст перед плашкой такой длины, что без защиты перенос пришёлся бы на пробел
    внутри подписи: первое слово подписи с пробелом влезает, остаток — нет."""
    first_word = label.split(" ", 1)[0] + " "
    room = len(first_word) + 1
    return "x" * (width - room - 1) + " "


def _visual_rows(inp) -> list[str]:
    return [
        inp.render_line(y).text for y in range(inp.wrapped_document.height)
    ]


@pytest.mark.asyncio
async def test_an_image_chip_moves_to_the_next_line_whole():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.text = _prefix_splitting(chip_text(1), inp.wrap_width)
        inp.move_cursor((0, len(inp.text)))
        inp.insert_image_chips([make_attachment("a" * 16, 1)], 1)
        await pilot.pause()
        start = inp.text.index(chip_text(1))
        end = start + len(chip_text(1))
        offsets = inp.wrapped_document.get_offsets(0)
        assert offsets  # строка переносится…
        assert not any(
            start < offset < end for offset in offsets
        )  # …но не внутри плашки
        assert any(
            chip_text(1) in row for row in _visual_rows(inp)
        )  # рисуется настоящий текст


@pytest.mark.asyncio
async def test_a_text_chip_moves_to_the_next_line_whole():
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = await _input(pilot)
        inp.text = _prefix_splitting(LABEL5, inp.wrap_width)
        inp.move_cursor((0, len(inp.text)))
        inp._handle_paste(BIG5)
        await pilot.pause()
        start = inp.text.index(LABEL5)
        end = start + len(LABEL5)
        offsets = inp.wrapped_document.get_offsets(0)
        assert offsets
        assert not any(start < offset < end for offset in offsets)
        assert inp.submission_text().endswith(
            BIG5
        )  # плашка по-прежнему разворачивается
