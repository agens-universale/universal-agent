"""Рендер-движок диффов: номера строк, маркеры, фоны, intraline, обрезка.

Проверяются и текстовый слепок (`.plain`), и стили (через `get_style_at_offset`).
"""

from __future__ import annotations

import time

import pytest

pytest.importorskip("rich")

from rich.color import Color
from rich.console import Console

from gc_tui.palette import (
    CODE_BLOCK,
    DIFF_ADD_BG,
    DIFF_ADD_GUTTER,
    DIFF_ADD_HL,
    DIFF_DEL_BG,
    DIFF_DEL_GUTTER,
    DIFF_DEL_HL,
    DIFF_GUTTER,
)
from gc_tui.services.diff_render import (
    _INTRALINE_MAX_LINE_CHARS,
    _INTRALINE_MAX_MATCH_WORK,
    _SYNTAX_MAX_LINES,
    _match_work,
    copyable_hunks,
    render_hunks,
)

CONSOLE = Console()

DIFFLIB_HUNKS = "--- \n+++ \n@@ -1,2 +1,2 @@\n line1\n-line2\n+line2x\n"

# Реальный вывод `git diff --no-ext-diff --no-textconv` (см. agent_edits/shell_delta).
GIT_HUNKS = (
    "diff --git a/tracked.txt b/tracked.txt\n"
    "index c0d0fb4..c18ba10 100644\n"
    "--- a/tracked.txt\n"
    "+++ b/tracked.txt\n"
    "@@ -1,2 +1,2 @@\n"
    " line1\n"
    "-line2\n"
    "+line2x\n"
)

# Реальный вывод `git diff --no-index -- /dev/null new.txt` для untracked-файла.
GIT_NEW_FILE_HUNKS = (
    "diff --git a/new.txt b/new.txt\n"
    "new file mode 100644\n"
    "index 0000000..fbbee86\n"
    "--- /dev/null\n"
    "+++ b/new.txt\n"
    "@@ -0,0 +1,2 @@\n"
    "+alpha\n"
    "+beta\n"
)


def _rows(render):
    """Строки рендера как есть (у залитых строк есть хвостовые пробелы заливки)."""
    return render.text.plain.split("\n")


def _shape(render):
    """Строки без хвостовой заливки — для проверки номеров/маркеров/содержимого."""
    return [row.rstrip() for row in _rows(render)]


def _row_start(render, index):
    rows = _rows(render)
    return sum(len(r) + 1 for r in rows[:index])


def _style_at(render, row_index, col):
    return render.text.get_style_at_offset(
        CONSOLE, _row_start(render, row_index) + col
    )


def _bg_at(render, row_index, col):
    return _style_at(render, row_index, col).bgcolor


# --- 1. difflib-форма: номера, маркеры, отсутствие служебных заголовков -------


def test_carriage_return_inside_a_line_does_not_cut_the_body():
    """Одиночный `\r` внутри строки диффа не обрывает тело.

    Разбор резал текст `str.splitlines`, а тот знает семь разделителей сверх
    `\n`. Строка с `\r` распадалась надвое: её хвост становился отдельной
    строкой без маркера, а разбор терял место в хэнке — номера уезжали, и всё,
    что шло дальше, приходило неверным (пятый раунд ревью, находка 3).
    """
    hunks = "@@ -1,1 +1,3 @@\n one\n+two\rthree\n+four\n"

    render = render_hunks(hunks, path="a.txt")

    # Сам `\r` в отрисовку не попадает — управляющий символ гасит rich,
    # и это его работа. Наше дело в том, что строка осталась ОДНОЙ, а тело
    # не оборвалось: `+four` на месте.
    assert _shape(render) == ["1  one", "2 +twothree", "3 +four"]


def test_difflib_form_numbers_markers_and_no_headers():
    render = render_hunks(DIFFLIB_HUNKS, path="tracked.txt")

    assert _shape(render) == ["1  line1", "2 -line2", "2 +line2x"]
    assert "--- " not in render.text.plain
    assert "+++" not in render.text.plain
    assert render.shown_lines == 3
    assert render.total_lines == 3
    assert render.truncated is False


def test_line_numbers_follow_hunk_header_offsets():
    hunks = "--- \n+++ \n@@ -5,3 +5,3 @@\n line4\n-line5\n+line5x\n line6\n"
    render = render_hunks(hunks, path="a.txt")
    assert _shape(render) == ["5  line4", "6 -line5", "6 +line5x", "7  line6"]


def test_hunk_header_without_counts_is_parsed():
    """`@@ -1 +1 @@` — difflib/git опускают счётчик, когда он равен единице."""
    render = render_hunks("--- \n+++ \n@@ -1 +1 @@\n-a\n+b\n", path="a.txt")
    assert _shape(render) == ["1 -a", "1 +b"]


def test_hunk_header_with_trailing_context_is_parsed():
    """`@@ ... @@ def foo():` — git дописывает имя охватывающей секции."""
    hunks = "--- \n+++ \n@@ -1,3 +1,3 @@ def foo():\n keep\n-old\n+new\n"
    render = render_hunks(hunks, path="a.py")
    assert _shape(render) == ["1  keep", "2 -old", "2 +new"]
    assert "def foo():" not in render.text.plain


def test_gutter_width_follows_widest_number():
    body = "".join(f" line{n}\n" for n in range(1, 11))
    render = render_hunks(
        f"--- \n+++ \n@@ -1,10 +1,10 @@\n{body}", path="a.txt"
    )
    rows = _rows(render)
    assert rows[0].startswith(" 1 ")  # право-выровненный гаттер шириной 2
    assert rows[9].startswith("10 ")


# --- 2. git-форма ------------------------------------------------------------


def test_git_form_body_matches_difflib_form():
    git = render_hunks(GIT_HUNKS, path="tracked.txt")
    difflib_form = render_hunks(DIFFLIB_HUNKS, path="tracked.txt")

    assert git.text.plain == difflib_form.text.plain
    for marker in ("diff --git", "index ", "a/tracked.txt", "b/tracked.txt"):
        assert marker not in git.text.plain


def test_git_new_file_headers_are_not_rendered():
    render = render_hunks(GIT_NEW_FILE_HUNKS, path="new.txt")

    assert _shape(render) == ["1 +alpha", "2 +beta"]
    for marker in ("new file mode", "index ", "/dev/null", "diff --git"):
        assert marker not in render.text.plain


# --- 3. фоновая заливка ------------------------------------------------------


def test_backgrounds_cover_whole_row_width():
    # длинная контекстная строка задаёт ширину блока — залитые строки добиваются
    # пробелами до неё, и фон обязан дожить до последнего символа
    hunks = (
        "--- \n+++ \n@@ -1,3 +1,3 @@\n"
        " a rather long context line\n"
        "-line2\n"
        "+line2x\n"
    )
    render = render_hunks(hunks, path="tracked.txt", syntax=False)
    rows = _rows(render)

    # контекст — без фона и без добивки
    assert rows[0] == "1  a rather long context line"
    assert _bg_at(render, 0, len(rows[0]) - 1) is None

    assert len(rows[1]) == len(rows[2]) == len(rows[0])
    assert rows[1].endswith(" ") and rows[2].endswith(" ")

    assert _bg_at(render, 1, 2) == Color.parse(DIFF_DEL_BG)
    assert _bg_at(render, 1, len(rows[1]) - 1) == Color.parse(DIFF_DEL_BG)

    assert _bg_at(render, 2, 2) == Color.parse(DIFF_ADD_BG)
    assert _bg_at(render, 2, len(rows[2]) - 1) == Color.parse(DIFF_ADD_BG)


def test_gutter_is_filled_with_row_background():
    """Полоса правки — ОДНА полоса: заливка накрывает и колонку номеров.

    Раньше она начиналась после гаттера, и номер сидел на фоне блока — строка
    читалась как текст с приклеенным слева чужим кусочком. Контраст номера
    гарантирует `test_markers_readable_on_their_row_backgrounds`: маркер того
    же цвета лежал на заливке и до этой правки.
    """
    render = render_hunks(DIFFLIB_HUNKS, path="tracked.txt")
    assert _bg_at(render, 2, 0) == Color.parse(DIFF_ADD_BG)
    # у контекстной строки заливки нет вовсе — и колонка остаётся на фоне блока
    assert _bg_at(render, 0, 0) is None


def test_fill_width_stretches_rows_to_requested_width():
    """`fill_width` — ширина залитой строки в ячейках (гаттер включён): виджет
    отдаёт сюда правый край подложки минус поле в один символ."""
    render = render_hunks(
        DIFFLIB_HUNKS, path="tracked.txt", syntax=False, fill_width=40
    )
    rows = _rows(render)

    assert len(rows[1]) == len(rows[2]) == 40
    assert _bg_at(render, 1, 39) == Color.parse(DIFF_DEL_BG)
    assert _bg_at(render, 2, 39) == Color.parse(DIFF_ADD_BG)
    # контекст заливки не получает ни при какой ширине
    assert rows[0] == "1  line1"
    assert _bg_at(render, 0, len(rows[0]) - 1) is None


def test_fill_width_below_content_wraps_and_loses_nothing():
    """Узкий блок содержимое НЕ теряет: длинная строка переносится.

    Прежний контракт был «шире ширины не режем, обрезкой занимается CSS» — и
    хвост строки пропадал молча, добраться до него было нельзя. Теперь строка
    продолжается на следующем ряду, и склейка рядов даёт исходное содержимое.
    """
    narrow = render_hunks(
        DIFFLIB_HUNKS,
        path="tracked.txt",
        syntax=False,
        fill_width=8,
        wrap=True,
    )
    rows = _rows(narrow)
    assert len(rows) > len(
        _rows(render_hunks(DIFFLIB_HUNKS, path="tracked.txt", syntax=False))
    )
    # содержимое добавленной строки собирается из рядов без потерь
    joined = "".join(
        row[3:].rstrip()
        for row in rows
        if row.lstrip().startswith(("2 +", "+"))
    )
    assert "line2x" in joined.replace(" ", "") or "line2x" in "".join(
        rows
    ).replace(" ", "")


def test_fill_width_none_keeps_content_width():
    """Дефолт (детальная карточка, движковые тесты) — прежнее поведение."""
    render = render_hunks(DIFFLIB_HUNKS, path="tracked.txt", syntax=False)
    assert len(_rows(render)[2]) == len("2 +line2x")


def test_gutter_number_and_marker_colored_by_row_kind():
    """Номер И маркер красятся по типу строки — структура правки считывается по
    колонке слева, не читая код. Контекст остаётся приглушённым."""
    render = render_hunks(DIFFLIB_HUNKS, path="tracked.txt")
    rows = _shape(render)
    assert rows == ["1  line1", "2 -line2", "2 +line2x"]
    marker_at = 2  # гаттер шириной 1 + пробел

    expected = [DIFF_GUTTER, DIFF_DEL_GUTTER, DIFF_ADD_GUTTER]
    for row, token in enumerate(expected):
        assert _style_at(render, row, 0).color == Color.parse(token), (
            f"номер {row}"
        )
        assert _style_at(render, row, marker_at).color == Color.parse(token), (
            f"маркер {row}"
        )


def test_marker_color_survives_row_background_and_syntax():
    """Маркер лежит НА заливке строки и рядом с подсвеченным кодом: заливка
    задаёт только bgcolor, а спаны синтаксиса до маркера не дотягиваются."""
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-def old(): pass\n+def new(): pass\n"
    render = render_hunks(hunks, path="mod.py")
    marker_at = 2
    assert (
        _rows(render)[0][marker_at] == "-"
        and _rows(render)[1][marker_at] == "+"
    )
    assert _style_at(render, 0, marker_at).color == Color.parse(
        DIFF_DEL_GUTTER
    )
    assert _style_at(render, 1, marker_at).color == Color.parse(
        DIFF_ADD_GUTTER
    )
    assert _bg_at(render, 0, marker_at) == Color.parse(DIFF_DEL_BG)
    assert _bg_at(render, 1, marker_at) == Color.parse(DIFF_ADD_BG)


def test_hunk_separator_uses_gutter_color():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-a\n+b\n@@ -20,1 +20,1 @@\n-c\n+d\n"
    render = render_hunks(hunks, path="a.txt")
    separator_row = next(i for i, r in enumerate(_rows(render)) if "⋮" in r)
    at = _rows(render)[separator_row].index("⋮")
    assert _style_at(render, separator_row, at).color == Color.parse(
        DIFF_GUTTER
    )
    assert _bg_at(render, separator_row, at) is None


# --- 4-5. intraline ----------------------------------------------------------


def test_intraline_marks_only_changed_fragment():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-x = 1\n+x = 2\n"
    render = render_hunks(hunks, path="a.txt", syntax=False)
    rows = _rows(render)

    del_row, add_row = rows[0], rows[1]
    assert del_row.startswith("1 -x = 1")
    assert add_row.startswith("1 +x = 2")

    # позиции: 0 — номер, 1 — пробел, 2 — маркер, 3 — 'x', 7 — цифра
    assert del_row[3] == "x" and del_row[7] == "1"
    assert _bg_at(render, 0, 7) == Color.parse(DIFF_DEL_HL)
    assert _bg_at(render, 0, 3) == Color.parse(DIFF_DEL_BG)

    assert add_row[3] == "x" and add_row[7] == "2"
    assert _bg_at(render, 1, 7) == Color.parse(DIFF_ADD_HL)
    assert _bg_at(render, 1, 3) == Color.parse(DIFF_ADD_BG)


def test_intraline_skipped_for_dissimilar_lines():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-aaaaaaaaaa\n+zzzzzzzzzz\n"
    render = render_hunks(hunks, path="a.txt", syntax=False)
    rows = _rows(render)

    del_bgs = {_bg_at(render, 0, i) for i in range(2, len(rows[0]))}
    add_bgs = {_bg_at(render, 1, i) for i in range(2, len(rows[1]))}
    assert del_bgs == {Color.parse(DIFF_DEL_BG)}
    assert add_bgs == {Color.parse(DIFF_ADD_BG)}


def _one_char_diff(old: str, changed_at: int, replacement: str):
    return old[:changed_at] + replacement + old[changed_at + 1 :]


def _pair_hunks(old: str, new: str) -> str:
    return f"--- \n+++ \n@@ -1,1 +1,1 @@\n-{old}\n+{new}\n"


def _accent_free(render, row_index, kind_bg):
    row = _rows(render)[row_index]
    return {_bg_at(render, row_index, i) for i in range(2, len(row))} == {
        Color.parse(kind_bg)
    }


# 215 РАЗНЫХ символов (печатный ASCII + кириллица + греческий). Длинная строка
# из такого пула стоит дёшево: работа матчинга ~ L²/215, поэтому на тысяче с
# лишним символов она остаётся под бюджетом — и потолок длины оказывается
# ЕДИНСТВЕННЫМ гвардом, который может сработать.
_MIXED_POOL = (
    [chr(code) for code in range(0x21, 0x7F)]
    + [chr(code) for code in range(0x410, 0x450)]
    + [chr(code) for code in range(0x391, 0x3CA)]
)


def _mixed_line(length: int) -> str:
    return "".join(_MIXED_POOL[i % len(_MIXED_POOL)] for i in range(length))


def test_intraline_applies_to_long_line_below_guards():
    """Длинная строка с повторяющейся структурой — там, где autojunk промахивался.

    310 символов (> 200, порога autojunk) из повторяющегося 32-символьного блока:
    `autojunk=True` даёт replace на 155 колонок, `autojunk=False` — ровно одну.
    Работа матчинга 3001 единица, то есть пара проходит оба гварда.
    """
    old = ("3YRWXENCTQJOP4F2LIBGHU5SDAZ67MVK" * 10)[:310]
    changed_at = 155
    new = _one_char_diff(old, changed_at, "0")
    render = render_hunks(_pair_hunks(old, new), path="a.txt", syntax=False)

    at = _rows(render)[0].index(old) + changed_at
    assert _bg_at(render, 0, at) == Color.parse(DIFF_DEL_HL)
    assert _bg_at(render, 1, at) == Color.parse(DIFF_ADD_HL)

    # заведомо одинаковые соседи различия акцент не получают
    for neighbour in (at - 1, at + 1):
        assert _bg_at(render, 0, neighbour) == Color.parse(DIFF_DEL_BG)
        assert _bg_at(render, 1, neighbour) == Color.parse(DIFF_ADD_BG)


def test_intraline_skipped_when_match_work_exceeds_budget():
    """Строка короче потолка длины, но с квадратичной стоимостью матчинга.

    301 символ из двух пробегов повторов: длина укладывается в
    `_INTRALINE_MAX_LINE_CHARS`, а работа (45000 единиц) кратно превышает бюджет —
    без бюджета такая пара стоила бы 6 мс. Одного потолка длины тут мало.
    """
    old = "a" * 150 + "X" + "b" * 150
    new = "a" * 150 + "Y" + "b" * 150
    assert len(old) < _INTRALINE_MAX_LINE_CHARS
    render = render_hunks(_pair_hunks(old, new), path="a.txt", syntax=False)

    assert _accent_free(render, 0, DIFF_DEL_BG)
    assert _accent_free(render, 1, DIFF_ADD_BG)


def test_intraline_skipped_for_very_long_line_under_work_budget():
    """Строка длиннее потолка, но с ДЕШЁВЫМ матчингом: гвард длины в одиночку.

    Единственное место, где `_INTRALINE_MAX_LINE_CHARS` реально проверяется: у
    прочих патологических строк первым срабатывает бюджет работы, и потолок
    длины можно было бы поднять до бесконечности, не покраснев ни одним тестом.
    """
    old = _mixed_line(_INTRALINE_MAX_LINE_CHARS + 1)
    new = _one_char_diff(old, 500, "§")
    assert (
        _match_work(old, new) < _INTRALINE_MAX_MATCH_WORK
    )  # бюджет ни при чём
    render = render_hunks(_pair_hunks(old, new), path="a.txt", syntax=False)

    assert _accent_free(render, 0, DIFF_DEL_BG)
    assert _accent_free(render, 1, DIFF_ADD_BG)


def test_intraline_applies_at_the_line_length_ceiling():
    """Ровно на потолке подсветка ещё работает — иначе тест выше был бы вакуумным."""
    old = _mixed_line(_INTRALINE_MAX_LINE_CHARS)
    new = _one_char_diff(old, 500, "§")
    render = render_hunks(_pair_hunks(old, new), path="a.txt", syntax=False)

    at = _rows(render)[0].index(old) + 500
    assert _bg_at(render, 0, at) == Color.parse(DIFF_DEL_HL)
    assert _bg_at(render, 1, at) == Color.parse(DIFF_ADD_HL)


def test_intraline_skipped_on_pathological_repeat_line():
    """Патологический вход: 2001 символ из повторов — без гварда 341 мс на пару."""
    old = "a" * 1000 + "X" + "b" * 1000
    new = "a" * 1000 + "Y" + "b" * 1000

    started = time.perf_counter()
    render = render_hunks(_pair_hunks(old, new), path="a.txt", syntax=False)
    elapsed = (time.perf_counter() - started) * 1000

    assert elapsed < 200
    assert _accent_free(render, 0, DIFF_DEL_BG)
    assert _accent_free(render, 1, DIFF_ADD_BG)


def test_intraline_pairs_rows_in_order_and_leaves_extra_rows_plain():
    hunks = (
        "--- \n+++ \n@@ -1,3 +1,2 @@\n-x = 1\n-y = 1\n-z = 1\n+x = 2\n+y = 2\n"
    )
    render = render_hunks(hunks, path="a.txt", syntax=False)
    rows = _rows(render)

    assert _bg_at(render, 0, 7) == Color.parse(DIFF_DEL_HL)  # -x = 1 ↔ +x = 2
    assert _bg_at(render, 1, 7) == Color.parse(DIFF_DEL_HL)  # -y = 1 ↔ +y = 2
    third = {_bg_at(render, 2, i) for i in range(2, len(rows[2]))}
    assert third == {Color.parse(DIFF_DEL_BG)}  # -z = 1 без пары


# --- 6. разделитель хэнков ---------------------------------------------------


def test_separator_between_hunks():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-a\n+b\n@@ -20,1 +20,1 @@\n-c\n+d\n"
    render = render_hunks(hunks, path="a.txt")
    rows = _rows(render)

    assert len(rows) == 5
    assert rows[2].strip() == "⋮"
    assert render.shown_lines == 4
    assert render.total_lines == 4


def test_no_separator_for_single_hunk():
    render = render_hunks(DIFFLIB_HUNKS, path="a.txt")
    assert "⋮" not in render.text.plain


def test_numbering_continues_correctly_across_multiline_hunks():
    hunks = (
        "--- \n+++ \n"
        "@@ -3,4 +3,4 @@\n"
        " line3\n"
        "-line4\n"
        "+line4x\n"
        " line5\n"
        " line6\n"
        "@@ -20,4 +20,5 @@\n"
        " line20\n"
        "-line21\n"
        "+line21x\n"
        "+line21y\n"
        " line22\n"
    )
    render = render_hunks(hunks, path="a.txt", syntax=False)

    assert _shape(render) == [
        " 3  line3",
        " 4 -line4",
        " 4 +line4x",
        " 5  line5",
        " 6  line6",
        " ⋮",
        "20  line20",
        "21 -line21",
        "21 +line21x",
        "22 +line21y",
        "23  line22",  # добавленная строка сдвинула нумерацию нового файла
    ]
    assert render.shown_lines == render.total_lines == 10


# --- 7. обрезка --------------------------------------------------------------


def test_truncation_reports_honest_counters():
    body = "".join(f" line{n}\n" for n in range(1, 11))
    render = render_hunks(
        f"--- \n+++ \n@@ -1,10 +1,10 @@\n{body}", path="a.txt", max_lines=3
    )

    assert render.shown_lines == 3
    assert render.total_lines == 10
    assert render.truncated is True
    assert len(_rows(render)) == 3
    assert "… ещё" not in render.text.plain


def test_no_truncation_when_limit_above_body():
    render = render_hunks(DIFFLIB_HUNKS, path="a.txt", max_lines=100)
    assert render.truncated is False
    assert render.shown_lines == render.total_lines == 3


# --- 8. синтаксис ------------------------------------------------------------


def test_syntax_colors_keywords_for_known_extension():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-def foo(): pass\n+def bar(): pass\n"
    render = render_hunks(hunks, path="mod.py")
    # 'd' ключевого слова def стоит сразу после маркера
    color = _style_at(render, 0, 3).color
    assert color is not None and color != Color.parse(CODE_BLOCK)


def test_syntax_off_keeps_plain_code_color():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-def foo(): pass\n+def bar(): pass\n"
    render = render_hunks(hunks, path="mod.py", syntax=False)
    assert _shape(render) == ["1 -def foo(): pass", "1 +def bar(): pass"]
    assert _style_at(render, 0, 3).color == Color.parse(CODE_BLOCK)


def _python_diff(lines: int):
    body = "".join(f"+def f{n}(): pass\n" for n in range(1, lines + 1))
    return f"--- \n+++ \n@@ -0,0 +1,{lines} @@\n{body}"


def test_syntax_stays_on_at_the_threshold():
    render = render_hunks(_python_diff(_SYNTAX_MAX_LINES), path="mod.py")
    assert render.shown_lines == _SYNTAX_MAX_LINES
    keyword_at = _rows(render)[0].index("def")
    assert _style_at(render, 0, keyword_at).color != Color.parse(CODE_BLOCK)


def test_syntax_disabled_automatically_above_threshold():
    render = render_hunks(_python_diff(_SYNTAX_MAX_LINES + 1), path="mod.py")
    assert render.shown_lines == _SYNTAX_MAX_LINES + 1
    keyword_at = _rows(render)[0].index("def")
    assert _style_at(render, 0, keyword_at).color == Color.parse(CODE_BLOCK)


# --- 9. пустой и мусорный вход -----------------------------------------------


@pytest.mark.parametrize(
    "hunks",
    [
        "",
        "   ",
        "не дифф\nвообще никак",
        "diff --git a/x b/x\nindex 111..222 100644\n",
    ],
)
def test_empty_and_garbage_input_render_empty(hunks):
    render = render_hunks(hunks, path="a.txt")
    assert render.text.plain == ""
    assert render.shown_lines == 0
    assert render.total_lines == 0
    assert render.truncated is False


def test_tabs_are_expanded_to_spaces():
    hunks = "--- \n+++ \n@@ -1,1 +1,1 @@\n-\tif x:\n+\tif y:\n"
    render = render_hunks(hunks, path="a.txt", syntax=False)
    assert _rows(render)[0].startswith("1 -    if x:")


# --- 10. рисование участками (Р23) -------------------------------------------
#
# Экраны правок рисуют тело не целиком, а теми строками, что доехали до кадра.
# Единственное, чем это позволено оплатить, — время: картинка обязана совпасть с
# нарисованной разом ДО СПАНА. Пары «удалено/добавлено» тут и есть ловушка:
# разбирать их надо целиком, а граница участка режет строки.


def _glued(hunks: str, step: int, **kwargs):
    """Тело, нарисованное кусками по `step` строк и склеенное обратно."""
    from rich.text import Text

    from gc_tui.services.diff_render import HunkBody

    body = HunkBody(hunks, **kwargs)
    glued = Text()
    for start in range(0, len(body.rows), step):
        if start:
            glued.append("\n")
        glued.append_text(body.render(start, start + step))
    return glued


def _spans(text):
    return [(span.start, span.end, str(span.style)) for span in text.spans]


@pytest.mark.parametrize("step", [1, 3, 7, 100])
def test_parts_glue_back_into_the_whole_render(step):
    """Склейка участков — тот же текст и те же стили, что у рендера целиком.

    Шаг подобран так, чтобы граница попадала и внутрь пары «удалено/добавлено»,
    и на разделитель хэнков: именно там слайс-рендер разъехался бы с целым.
    """
    hunks = (
        "@@ -1,6 +1,6 @@\n ctx1\n-старая строка один\n-старая строка два\n"
        "+новая строка один\n+новая строка два\n ctx2\n"
        "@@ -40,4 +40,4 @@\n ctx3\n-alpha beta\n+alpha gamma\n ctx4\n"
    )
    whole = render_hunks(hunks, path="a.txt")
    glued = _glued(hunks, step, path="a.txt")
    assert glued.plain == whole.text.plain
    assert _spans(glued) == _spans(whole.text)


def test_intraline_survives_a_border_inside_the_pair():
    """Акцент внутри пары не зависит от того, где прошла граница участка.

    Пара разбирается целиком, поэтому диапазон расширяется до краёв группы;
    посчитай движок акценты по строкам участка — вторая половина пары осталась
    бы без подсветки, и одинаковый дифф выглядел бы по-разному в двух местах.
    """
    hunks = "@@ -1,4 +1,4 @@\n-alpha one\n-alpha two\n+alpha ONE\n+alpha TWO\n"
    whole = render_hunks(hunks, path="a.txt")
    # Граница ровно между удалёнными и добавленными строками пары.
    glued = _glued(hunks, 2, path="a.txt")
    assert _spans(glued) == _spans(whole.text)
    assert any(str(DIFF_ADD_HL) in style for _s, _e, style in _spans(glued))


def test_line_count_is_known_without_drawing():
    """Число строк тела известно из разбора — рисовать для этого не нужно.

    На этом стоит ленивая сборка: место под участок резервируется до того, как
    он нарисован.
    """
    from gc_tui.services.diff_render import HunkBody

    hunks = "@@ -1,3 +1,3 @@\n ctx\n-old\n+new\n@@ -40,2 +40,2 @@\n-a\n+b\n"
    body = HunkBody(hunks, path="a.txt")
    assert len(body.kinds) == len(
        render_hunks(hunks, path="a.txt").text.plain.splitlines()
    )
    assert body.kinds.count("sep") == 1  # разделитель тоже занимает строку


# --- копия для внешних инструментов (Ж5 батча 4) ----------------------------

_COPY_DIFFLIB = "--- \n+++ \n@@ -1,2 +1,2 @@\n-старое\n+новое\n"


def test_difflib_headers_get_the_file_name():
    out = copyable_hunks(_COPY_DIFFLIB, path="src/app.py")
    lines = out.split("\n")
    assert lines[0] == "--- a/src/app.py"
    assert lines[1] == "+++ b/src/app.py"
    assert out.endswith("+новое\n")  # тело не тронуто


def test_windows_separators_become_forward_slashes():
    out = copyable_hunks(_COPY_DIFFLIB, path="src\\tui\\app.py")
    assert out.split("\n")[0] == "--- a/src/tui/app.py"


def test_git_form_is_returned_as_is():
    git_form = (
        "diff --git a/x.py b/x.py\nindex 1..2 100644\n"
        "--- a/x.py\n+++ b/x.py\n@@ -1 +1 @@\n-a\n+b\n"
    )
    assert copyable_hunks(git_form, path="x.py") == git_form


def test_unparsable_input_is_returned_as_is():
    assert copyable_hunks("", path="x.py") == ""
    assert copyable_hunks("просто текст\n", path="x.py") == "просто текст\n"


def test_absolute_path_leaves_the_headers_alone():
    # `canonical_path_key` штатно отдаёт АБСОЛЮТНЫЙ ключ любому файлу ВНЕ
    # проекта, а `git apply -p1` не примет `--- a/C:/…`. Дифф без имени
    # честнее диффа с именем, которое инструмент отвергнет.
    assert (
        copyable_hunks(_COPY_DIFFLIB, path="C:\\out\\app.py") == _COPY_DIFFLIB
    )
    assert (
        copyable_hunks(_COPY_DIFFLIB, path="/srv/data/app.py") == _COPY_DIFFLIB
    )


# --- Перенос длинных строк (дизайн 2026-09-09) --------------------------------


_LONG = "x" * 60


def _wrapped(fill_width, *, max_lines=None):
    hunks = "--- \n+++ \n@@ -1,2 +1,2 @@\n short\n+" + _LONG + "\n"
    return render_hunks(
        hunks,
        path="a.txt",
        syntax=False,
        wrap=True,
        fill_width=fill_width,
        max_lines=max_lines,
    )


def test_long_line_wraps_instead_of_being_cut():
    """Хвост длинной строки не пропадает: она продолжается на следующем ряду.

    Перенос делаем САМИ, а не просим у Rich: его `overflow="fold"` рвёт сначала
    по словам и складывает только слишком длинное слово — на коде это оставляет
    первое слово одиноко на ряду и тратит ряды впустую.
    """
    rows = _rows(_wrapped(20))
    assert len(rows) > 2  # контекст + минимум два ряда правки
    assert "".join(row[3:].rstrip() for row in rows[1:]) == _LONG


def test_number_prints_once_and_marker_on_every_row():
    """Номер опознаёт строку ФАЙЛА — повторённый, он соврал бы. Маркер, наоборот,
    нужен на каждом ряду: он говорит, какого рода строку читаешь."""
    rows = _rows(_wrapped(20))
    tail = rows[1:]
    assert tail[0].startswith("2 +")
    for row in tail[1:]:
        assert row.startswith("  +"), row  # колонка пуста, маркер на месте


def test_budget_counts_screen_rows_not_file_lines():
    """Лимит — сколько ВЕРТИКАЛИ блок вправе занять. Считать логические строки
    значило бы позволить блоку из длинных строк развернуться втрое."""
    assert len(_rows(_wrapped(20, max_lines=3))) <= 3


def test_cut_falls_on_a_logical_line_boundary():
    """Половина перенесённой строки не показывается никогда — иначе сноска
    «ещё N строк» не может быть точной."""
    rows = _rows(_wrapped(20, max_lines=3))
    tail = [row for row in rows if row.lstrip().startswith("+")]
    if tail:
        assert "".join(row[3:].rstrip() for row in tail) == _LONG


def test_footnote_counts_logical_lines():
    """«ещё N строк» — про строки файла: экранные ряды зависят от ширины окна и
    менялись бы при её изменении."""
    render = _wrapped(20, max_lines=1)
    assert render.total_lines == 2  # short + длинная
    assert render.shown_lines == 1  # влезла только короткая
    assert render.truncated


def test_wrapped_fill_does_not_blow_up_on_a_huge_line():
    """Заливка при переносе — по ширине ПОКАЗА, а не по длине логической строки.

    Иначе каждый кусок добивался пробелами до длины самой длинной строки: 100 000
    знаков давали 1031 ряд по 100 000, то есть текст на 103 млн знаков и около
    200 МБ на один блок ленты.
    """
    huge = "@@ -1,1 +1,1 @@\n+" + "x" * 100_000
    render = render_hunks(
        huge, path="a.js", syntax=False, fill_width=100, wrap=True
    )
    plain = render.text.plain
    rows = plain.count("\n") + 1
    assert rows > 900  # строка действительно перенеслась
    assert len(plain) < 2 * 100_000  # и текст остался соразмерен ей


def test_single_long_line_cannot_blow_the_row_budget():
    """Первая строка берётся всегда, но не сверх бюджета.

    Правило «первую берём всегда» задумано против пустого тела с одной сноской.
    Одна длинная строка переполняла бюджет не «на ряд», а вдесятеро: 30 рядов
    превращались в 310.
    """
    hunks = "@@ -1,2 +1,2 @@\n+" + "y" * 30_000 + "\n+короткая"
    render = render_hunks(
        hunks,
        path="a.js",
        syntax=False,
        fill_width=100,
        wrap=True,
        max_lines=30,
    )
    assert render.text.plain.count("\n") + 1 <= 30


def test_single_long_line_cut_by_the_row_cap_is_reported_as_truncated():
    """Тело из ОДНОЙ длинной строки: рез прошёл внутри неё, а не по строкам.

    `shown_lines`/`total_lines` такого реза не видят — оба равны единице, и
    блок отдавал `truncated=False`: хвоста «… ещё N строк» не было, а
    содержимое молча обрезано и развернуть его нечем.
    """
    hunks = "@@ -1,1 +1,1 @@\n+" + "x" * 4000 + "\n"
    render = render_hunks(
        hunks, path="a.py", max_lines=30, fill_width=80, wrap=True
    )

    assert render.total_lines == render.shown_lines == 1
    assert render.hidden_rows > 0
    assert render.truncated is True
    assert len(render.text.plain.split("\n")) == 30


def test_syntax_skips_only_the_over_long_lines_not_the_whole_body():
    """Отсечка подсветки по ДЛИНЕ строки, и гасит она строку, а не тело.

    Тело из трёх строк по 9 тыс. символов проходит лимит по ЧИСЛУ строк
    насквозь, а лексер выдаёт на нём тысячи спанов: Rich складывает их на
    КАЖДУЮ отрисовку, и развёрнутый блок в ленте залипает на скролле (замер:
    265 мс против 2 мс без подсветки). Но соседние нормальные строки ни в чём
    не виноваты — цвет у них обязан остаться.
    """
    from gc_tui.services.diff_render import _syntax_spans

    over_long = " + ".join(f'"chunk_{n:04d}"' for n in range(600))
    per_line = _syntax_spans(["def build_payload():", over_long], "python")

    assert per_line[0], "обычная строка обязана остаться цветной"
    assert per_line[1] == [], "строка сверх предела не красится"


def test_crlf_line_endings_are_not_line_content():
    """CRLF-файл: `\r` — часть перевода строки, а не текст строки.

    Разбор режет дифф по `\n`, и `\r` оставался в содержимом: заливка считалась
    с ним, а при выводе управляющий символ выбрасывался — залитая строка
    выходила на клетку короче соседних («лесенка»), а внутристрочная подсветка
    метила `\r` изменённым символом."""
    from gc_tui.services.diff_render import HunkBody, _parse

    hunks = (
        "--- \n+++ \n@@ -1,3 +1,4 @@\n one\r\n two\r\n"
        "-three\r\n+three!\r\n+four\n"
    )
    assert all("\r" not in row.content for row in _parse(hunks))

    rows = (
        HunkBody(hunks, path="a.txt", fill_width=40).render().plain.split("\n")
    )
    filled = [r for r in rows if r[2:3] in ("+", "-")]
    assert len(filled) == 3
    assert len({len(r) for r in filled}) == 1, filled


# --- свёрнутое превью под блоком ленты: один бюджет строк на файл -------
# (решения владельца 2026-10-23 и 2026-10-26, спека P4 §6.3): дифф с
# контекстом влезает — целиком, как раскрытый; иначе все изменённые и
# ближайший к ним контекст; изменённых больше бюджета — первые изменённые.

CTX_HUNKS = (
    "--- \n+++ \n"
    "@@ -1,7 +1,7 @@\n a\n b\n c\n-d\n+D\n e\n f\n g\n"
    "@@ -40,5 +40,6 @@\n x\n y\n-z1\n-z2\n+Z1\n+Z2\n+Z3\n w\n"
)


def _shown(render) -> list[str]:
    return [row.rstrip() for row in render.text.plain.split("\n")]


def _preview(hunks: str, limit, path: str = "a.txt"):
    return render_hunks(
        hunks, path=path, max_lines=limit, logical=True, preview=True
    )


def _full(hunks: str) -> list[str]:
    return _shown(render_hunks(hunks, path="a.txt", logical=True))


def _rows_of(hunks: str, *picked: int) -> list[str]:
    """Строки полного тела по позициям: номера и `⋮` — как раскрыто."""
    full = _full(hunks)
    return [full[i] for i in picked]


@pytest.mark.parametrize("limit", [16, 30, None])
def test_preview_fits_whole_like_expanded(limit):
    render = _preview(CTX_HUNKS, limit)
    assert _shown(render) == _full(CTX_HUNKS)
    assert not render.truncated


def test_preview_one_line_edit_is_whole():
    """Правка одной строки с контекстом (8 строк) при бюджете 10 — целиком,
    скрытого нет."""
    hunks = "@@ -7,7 +7,7 @@\n c7\n c8\n c9\n-old\n+new\n c11\n c12\n c13\n"
    render = _preview(hunks, 10)
    assert _shown(render) == _full(hunks)
    assert render.shown_lines == render.total_lines == 8


def test_preview_six_changed_take_nearest_context():
    """6 изменённых и 6 контекста при бюджете 10: изменённые и по две
    ближайшие строки контекста сверху и снизу, крайние уходят."""
    hunks = "@@ -1,9 +1,9 @@\n a\n b\n c\n-1\n-2\n-3\n+1\n+2\n+3\n e\n f\n g\n"
    render = _preview(hunks, 10)
    assert _shown(render) == _full(hunks)[1:11]
    assert (render.shown_lines, render.total_lines) == (10, 12)


def test_preview_context_spread_over_hunks():
    """Два хэнка, бюджет 10 = 7 изменённых и 3 контекста: ближайшие к
    изменениям строки, поровну между хэнками и сторонами; номера и `⋮`
    — как в полном теле."""
    render = _preview(CTX_HUNKS, 10)
    assert _shown(render) == _rows_of(
        CTX_HUNKS, 2, 3, 4, 5, 8, 11, 12, 13, 14, 15, 16
    )
    assert render.shown_lines == 10


def test_preview_next_context_ring():
    """Бюджет 12: весь первый круг контекста (4) и одна строка второго."""
    render = _preview(CTX_HUNKS, 12)
    assert _shown(render) == _rows_of(
        CTX_HUNKS, 1, 2, 3, 4, 5, 8, 10, 11, 12, 13, 14, 15, 16
    )


def test_preview_context_between_changes_in_hunk():
    hunks = "@@ -1,8 +1,8 @@\n-a\n+A\n m1\n m2\n m3\n m4\n-b\n+B\n"
    render = _preview(hunks, 6)
    assert _shown(render) == _rows_of(hunks, 0, 1, 2, 5, 6, 7)


def test_preview_changed_over_budget_takes_first_changed():
    render = _preview(CTX_HUNKS, 3)
    assert _shown(render) == [" 4 -d", " 4 +D", " ⋮", "42 -z1"]
    assert render.shown_lines == 3


@pytest.mark.parametrize("count,shown", [(5, 5), (12, 10)])
def test_preview_new_file_same_rule(count, shown):
    hunks = f"@@ -0,0 +1,{count} @@" + "".join(
        f"\n+l{i}" for i in range(count)
    )
    render = _preview(hunks, 10)
    assert _shown(render) == _full(hunks)[:shown]


@pytest.mark.parametrize("limit", range(1, 21))
def test_preview_shows_min_of_lines_and_budget(limit):
    """Показано всегда `min(строк, бюджет)` — на этом счёте модель строит
    `+ ещё N строк`."""
    assert _preview(CTX_HUNKS, limit).shown_lines == min(16, limit)


def test_preview_gutter_matches_full_body():
    """Колонка номеров — по всему телу: раскрытие её не сдвигает."""
    short = (
        "--- \n+++ \n@@ -1,3 +1,3 @@\n-a\n+b\n c\n@@ -99,2 +99,2 @@\n x\n y\n"
    )
    assert _shown(_preview(short, 2)) == ["  1 -a", "  1 +b"]


def _accent_cells(render, line: int) -> list[int]:
    """Клетки строки ``line`` с внутристрочным акцентом."""
    row = render.text.plain.split("\n")[line]
    hl = {Color.parse(DIFF_DEL_HL), Color.parse(DIFF_ADD_HL)}
    return [i for i in range(len(row)) if _bg_at(render, line, i) in hl]


def test_preview_pairs_accents_like_full_hunk():
    """Пары «удалено / добавлено» для акцентов — по полному хэнку: удаление,
    контекст, затем пара −/+ — свёрнуто подсвечено как раскрыто."""
    hunks = (
        "--- \n+++ \n@@ -1,4 +1,3 @@\n"
        "-total = 3\n keep = 0\n-total = 1\n+total = 2\n tail\n"
    )
    full = render_hunks(hunks, path="a.py", syntax=False)
    short = render_hunks(
        hunks, path="a.py", syntax=False, max_lines=3, preview=True
    )
    # Свёрнуто: строки 0, 1, 2 — это строки 0, 2, 3 полного тела.
    for short_line, full_line in ((0, 0), (1, 2), (2, 3)):
        assert _accent_cells(short, short_line) == _accent_cells(
            full, full_line
        )
    assert _accent_cells(full, 0) == []
    assert _accent_cells(full, 2) != []


def test_copied_delete_patch_applies_with_git(tmp_path):
    """Скопированная запись удаления шелл-канала — патч в форме git:
    ``git apply`` удаляет файл (решение ревью 2026-11-05)."""
    import shutil
    import subprocess

    from gc_core.agent_edits.delete_records import DeleteBudget, delete_record

    if not shutil.which("git"):
        pytest.skip("git недоступен")
    key = "sub/a.txt"
    content = b"one\ntwo\nthree\n"
    (tmp_path / "sub").mkdir()
    (tmp_path / key).write_bytes(content)
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    record = delete_record(
        key,
        "t1",
        content,
        mode=0o644,
        budget=DeleteBudget(1000),
        context_lines=3,
        max_chars=0,
    )
    patch = tmp_path / "rm.patch"
    patch.write_text(copyable_hunks(record.hunks, path=key), encoding="utf-8")

    subprocess.run(
        ["git", "apply", str(patch)],
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )

    assert not (tmp_path / key).exists()
    assert render_hunks(record.hunks, path=key).total_lines == 3
