"""Раскладка реплики пользователя в ленте: перенос строк и копирование выделенного.

Перенос: плашка картинки не рвётся, строки-продолжения начинаются под текстом.
Копирование: из выделения получается текст сообщения как написан — без префиксов,
отступов и переносов, которые добавила раскладка.
"""

import pytest
from rich.cells import cell_len
from rich.style import Style
from rich.text import Text

from gc_tui.widgets.reply_layout import LineSpec, ReplyLayout

CHIP = "[Изображение #6]"


def _spec(plain, *, prefix=2, indent=2, chips=()):
    return LineSpec(plain, tuple(chips), prefix, indent)


def _chip_spec(plain, *, prefix=2, indent=2):
    start = plain.index(CHIP)
    return _spec(
        plain,
        prefix=prefix,
        indent=indent,
        chips=[(start, start + len(CHIP))],
    )


def _rows(specs, width):
    """Строки отрисовки; префикс первой строки — `>` и пробелы нужной ширины."""
    layout = ReplyLayout(specs, width)
    prefixes = [Text(">".ljust(spec.prefix)) for spec in specs]
    bodies = [Text(spec.plain) for spec in specs]
    return layout.render(prefixes, bodies).plain.split("\n")


def _copy(specs, width, start, end):
    return ReplyLayout(specs, width).copy(start, end)


# ---- перенос ----


def test_zero_width_keeps_logical_lines():
    specs = [_spec("первая строка"), _spec("вторая")]
    assert _rows(specs, 0) == ["> первая строка", "> вторая"]


def test_continuation_rows_start_under_the_text():
    assert _rows([_spec("aaa bbb ccc")], 9) == ["> aaa bbb", "  ccc"]


def test_no_row_is_wider_than_the_width():
    text = "проверь пожалуйста что в этом файле и почему тест падает " * 3
    for width in range(4, 40):
        rows = _rows([_spec(text)], width)
        assert all(cell_len(row) <= width for row in rows), width


def test_chip_moves_to_the_next_row_whole():
    plain = f"глянь {CHIP} тут"
    assert _rows([_chip_spec(plain)], 20) == ["> глянь", f"  {CHIP}", "  тут"]


def test_chip_wider_than_the_row_is_folded_and_copied_whole():
    plain = f"a {CHIP} b"
    specs = [_chip_spec(plain)]
    rows = _rows(specs, 10)
    assert all(cell_len(row) <= 10 for row in rows)
    assert "".join(row[2:] for row in rows[1:-1]) == CHIP
    assert _copy(specs, 10, None, None) == plain


def test_long_word_is_folded():
    assert _rows([_spec("abcdefghij")], 6) == ["> abcd", "  efgh", "  ij"]


def test_wide_characters_count_by_cells():
    assert _rows([_spec("中文字符号")], 6) == ["> 中文", "  字符", "  号"]
    assert _rows([_spec("😀😀😀")], 6) == ["> 😀😀", "  😀"]


def test_blank_and_whitespace_lines_are_kept():
    specs = [_spec("a"), _spec(""), _spec("b")]
    assert _rows(specs, 10) == ["> a", "> ", "> b"]
    assert _copy(specs, 10, None, None) == "a\n\nb"


def test_trailing_spaces_render_while_they_fit():
    assert _rows([_spec("ab  ")], 10) == ["> ab  "]
    rows = _rows([_spec("ab" + " " * 20)], 10)
    assert rows == ["> ab      "]
    assert _copy([_spec("ab" + " " * 20)], 10, None, None) == "ab" + " " * 20


def test_tab_is_expanded_on_screen_and_restored_in_the_copy():
    specs = [_spec("a\tb")]
    assert _rows(specs, 40) == ["> a       b"]
    assert _copy(specs, 40, None, None) == "a\tb"
    assert _copy(specs, 40, (10, 0), (11, 0)) == "b"


def test_failed_reply_continues_under_the_text():
    rows = _rows([_spec("aaa bbb", prefix=6, indent=6)], 10)
    assert rows == [">     aaa", "      bbb"]


def test_image_line_continues_under_its_chip():
    plain = f"└ {CHIP} · png 1456×819 · скрин.png"
    rows = _rows([_chip_spec(plain, prefix=2, indent=4)], 21)
    assert rows == [f"> └ {CHIP}", "    · png 1456×819 ·", "    скрин.png"]


def test_chip_meta_survives_folding():
    style = Style(meta={"image_id": "img-6"})
    plain = f"{CHIP}"
    spec = _chip_spec(plain)
    body = Text()
    body.append(CHIP, style=style)
    rendered = ReplyLayout([spec], 10).render([Text("> ")], [body])
    lines = rendered.split("\n")
    assert len(lines) > 1
    for line in lines:
        assert [s.style.meta["image_id"] for s in line.spans] == ["img-6"]


# ---- сокращённый показ (путь картинки) ----

FULL = f"└ {CHIP} · /Users/m/Desktop/screens/shot.png"
SHORT = f"└ {CHIP} · ~/…/shot.png"


def _short_spec():
    start = FULL.index(CHIP)
    return LineSpec(FULL, ((start, start + len(CHIP)),), 2, 4, SHORT)


def test_shown_text_is_drawn_and_wrapped_by_its_own_width():
    assert _rows([_short_spec()], len(SHORT) + 2) == [f"> {SHORT}"]


def test_copy_restores_the_hidden_part():
    spec = _short_spec()
    assert _copy([spec], 80, None, None) == FULL
    ellipsis = 2 + SHORT.index("…")
    assert _copy([spec], 80, (ellipsis, 0), (ellipsis + 1, 0)) == (
        "/Users/m/Desktop/screens"
    )


def test_shown_text_keeps_the_styles_of_the_hidden_part():
    spec = _short_spec()
    body = Text(FULL[: FULL.index("/Users")])
    body.append(
        FULL[FULL.index("/Users") :], style=Style(meta={"image_id": "x"})
    )
    rendered = ReplyLayout([spec], 80).render([Text("> ")], [body])
    assert rendered.plain == f"> {SHORT}"
    ellipsis = 2 + SHORT.index("…")
    assert [
        s.style.meta["image_id"]
        for s in rendered.spans
        if s.start <= ellipsis < s.end
    ] == ["x"]


# ---- копирование ----


def test_copy_part_of_a_row():
    assert _copy([_spec("aaa bbb ccc")], 9, (2, 0), (5, 0)) == "aaa"


def test_copy_across_a_soft_wrap_keeps_the_space_not_a_newline():
    assert _copy([_spec("aaa bbb ccc")], 9, (6, 0), (5, 1)) == "bbb ccc"


def test_copy_across_message_lines_keeps_the_newline():
    specs = [_spec("aaa"), _spec("bbb")]
    assert _copy(specs, 20, (2, 0), (5, 1)) == "aaa\nbbb"


def test_copy_start_in_the_prefix_starts_at_the_text():
    assert _copy([_spec("aaa bbb ccc")], 9, (0, 1), (5, 1)) == "ccc"


def test_copy_end_in_the_prefix_ends_at_the_previous_row():
    specs = [_spec("aaa bbb ccc")]
    assert _copy(specs, 9, (2, 0), (1, 1)) == "aaa bbb"
    assert _copy([_spec("aaa"), _spec("bbb")], 20, (2, 0), (1, 1)) == "aaa"


def test_copy_open_ends_and_out_of_range():
    specs = [_spec("aaa bbb ccc")]
    assert _copy(specs, 9, None, (5, 0)) == "aaa"
    assert _copy(specs, 9, (6, 0), None) == "bbb ccc"
    assert _copy(specs, 9, (0, 9), None) == ""


def test_copy_of_the_image_line_is_its_text():
    specs = [_spec("глянь"), _chip_spec(f"└ {CHIP} · png", indent=4)]
    assert _copy(specs, 12, None, None) == f"глянь\n└ {CHIP} · png"


@pytest.mark.parametrize(
    "lines",
    [
        ["простая реплика"],
        [f"текст {CHIP} и ещё текст", "", "  отступ\tтаб", "中文 😀 end  "],
        ["a" * 50, "b c " * 20],
    ],
)
def test_select_all_copies_the_message_at_any_width(lines):
    specs = [_spec(line) for line in lines]
    for width in (0, 1, 3, 5, 8, 13, 21, 80):
        assert _copy(specs, width, None, None) == "\n".join(lines)
