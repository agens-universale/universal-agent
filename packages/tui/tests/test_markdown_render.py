"""segment_answer: сегментация ответа на прозовые прогоны + code/table-блоки."""

from gc_tui.services.markdown_render import (
    Block,
    build_markdown_table,
    render_prose,
    segment_answer,
    table_block_to_markdown,
)


def test_pure_prose_is_one_run():
    assert segment_answer("просто текст") == [Block("prose", "просто текст")]


def test_blank_lines_preserved_inside_run():
    assert segment_answer("абзац1\n\nабзац2") == [
        Block("prose", "абзац1\n\nабзац2")
    ]


def test_heading_and_bullet_stay_in_prose_run():
    # синтаксис НЕ срезаем — render_prose отрисует # и - построчно
    assert segment_answer("# Title\n- item") == [
        Block("prose", "# Title\n- item")
    ]


def test_edge_blank_lines_trimmed():
    assert segment_answer("\n\nтекст\n\n") == [Block("prose", "текст")]


def test_fenced_with_lang_is_code_block():
    assert segment_answer("```python\nx = 1\ny = 2\n```") == [
        Block("code-block", "x = 1\ny = 2", "python")
    ]


def test_fenced_no_lang():
    assert segment_answer("```\nx = 1\n```") == [
        Block("code-block", "x = 1", None)
    ]


def test_mixed_prose_and_code_splits_runs():
    text = "Вот пример:\n```js\nconst x = 1;\n```\nКонец."
    assert segment_answer(text) == [
        Block("prose", "Вот пример:"),
        Block("code-block", "const x = 1;", "js"),
        Block("prose", "Конец."),
    ]


def test_table_basic():
    segs = segment_answer("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
    assert len(segs) == 1 and segs[0].kind == "table"
    assert segs[0].headers == ["A", "B"]
    assert segs[0].rows == [["1", "2"], ["3", "4"]]
    assert segs[0].aligns == ["left", "left"]


def test_table_alignment_markers():
    segs = segment_answer("| L | C | R |\n|:--|:-:|--:|\n| a | b | c |")
    assert segs[0].aligns == ["left", "center", "right"]


def test_pipe_line_without_separator_is_prose():
    assert segment_answer("a | b | c\nплейн текст") == [
        Block("prose", "a | b | c\nплейн текст")
    ]


def test_table_ragged_row_padded():
    segs = segment_answer("| A | B | C |\n|---|---|---|\n| 1 | 2 |")
    assert segs[0].rows == [["1", "2", ""]]


def test_build_markdown_table_smoke():
    t = build_markdown_table(["A", "B"], [["1", "2"]], ["left", "left"])
    assert t.columns[0].header == "A"


def test_build_markdown_table_column_count_and_justify():
    """Columns count matches headers; right-justified column has justify='right'."""
    t = build_markdown_table(
        ["Name", "Score"], [["Alice", "99"]], ["left", "right"]
    )
    assert len(t.columns) == 2
    assert t.columns[0].justify == "left"
    assert t.columns[1].justify == "right"


def test_build_markdown_table_header_style():
    """Заголовок колонки — `INK` жирный: всё выделение — INK жирный."""
    from gc_tui.palette import INK

    t = build_markdown_table(["Col"], [["val"]], ["left"])
    assert t.columns[0].header_style == f"{INK} bold"


def test_build_markdown_table_inline_code_cell():
    """Cell containing `code` spans: backticks stripped, text rendered with code style (#afd7ff)."""
    t = build_markdown_table(["H"], [["`snippet`"]], ["left"])
    # The cell is a rich.Text; iterate spans to find code-styled segment.
    cell = t.columns[0]._cells[0]  # first row, first column
    styles = [span.style for span in cell._spans]
    assert any("#afd7ff" in str(s) for s in styles), (
        f"Expected code style #afd7ff in cell spans, got: {styles}"
    )


# ---- table_block_to_markdown: обратная сборка (таблица → копируемый markdown) ----


def test_table_block_to_markdown_basic():
    """3 колонки / 2 строки → валидный markdown с header + separator + rows."""
    md = table_block_to_markdown(
        ["A", "B", "C"],
        [["1", "2", "3"], ["4", "5", "6"]],
        ["left", "left", "left"],
    )
    lines = md.split("\n")
    assert lines[0] == "| A | B | C |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| 1 | 2 | 3 |"
    assert lines[3] == "| 4 | 5 | 6 |"
    assert len(lines) == 4


def test_table_block_to_markdown_aligns():
    """left → ``---``, center → ``:---:``, right → ``---:``."""
    md = table_block_to_markdown(
        ["L", "C", "R"],
        [["a", "b", "c"]],
        ["left", "center", "right"],
    )
    sep = md.split("\n")[1]
    assert sep == "| --- | :---: | ---: |"


def test_table_block_to_markdown_round_trip():
    """Round-trip: segment_answer(table_block_to_markdown(...)) == исходный Block
    на «чистых» ячейках (без ``|``)."""
    headers = ["Name", "Score"]
    rows = [["Alice", "99"], ["Bob", "42"]]
    aligns = ["left", "right"]
    md = table_block_to_markdown(headers, rows, aligns)
    segs = segment_answer(md)
    assert len(segs) == 1 and segs[0].kind == "table"
    assert segs[0].headers == headers
    assert segs[0].rows == rows
    # aligns: round-trip теряет left/right-различие (``---`` vs ``---:``),
    # но left остаётся left, right — right.
    assert segs[0].aligns == ["left", "right"]


def test_table_block_to_markdown_escape_pipe():
    """Ячейка ``a|b`` → в выводе ``a\\|b`` (без round-trip через парсер)."""
    md = table_block_to_markdown(["H"], [["a|b"]], ["left"])
    assert r"a\|b" in md
    # строка ячейки содержит экранированную pipe, не разрывает колонку
    cell_line = md.split("\n")[2]
    assert cell_line == "| a\\|b |"


def test_table_block_to_markdown_empty():
    """Пустые headers/rows → только header + separator, без строк данных.
    Крайний случай: ``" | ".join([])`` = "" → строка ``"|  |"``."""
    md = table_block_to_markdown([], [], [])
    lines = md.split("\n")
    assert lines[0] == "|  |"  # "| " + "" + " |"
    assert lines[1] == "|  |"
    assert len(lines) == 2


# ---- Е2: вложенные ограды (CommonMark), пустой блок без сегмента ----


def test_nested_fence_with_info_string_does_not_close_outer_block():
    text = (
        "Итог:\n```markdown\n# T\n\n```bash\npip install -e .\n```\n```\nхвост"
    )
    segs = segment_answer(text)
    kinds = [s.kind for s in segs]
    assert kinds == ["prose", "code-block", "code-block"]
    assert segs[1].lang == "markdown"
    # строка ```bash — содержимое, а не закрывающая ограда; закрыл блок первый голый ```
    assert segs[1].content == "# T\n\n```bash\npip install -e ."
    # второй голый ``` открывает блок без закрывающей ограды → тянется до конца текста
    assert segs[2].content == "хвост"
    assert segs[2].lang is None


def test_four_backtick_fence_closes_only_on_four():
    text = "````md\n```py\nx = 1\n```\n````\n"
    segs = segment_answer(text)
    assert [s.kind for s in segs] == ["code-block"]
    assert segs[0].content == "```py\nx = 1\n```"


def test_unclosed_fence_runs_to_the_end():
    segs = segment_answer("```\nabc\ndef")
    assert [s.kind for s in segs] == ["code-block"]
    assert segs[0].content == "abc\ndef"


def test_empty_fenced_block_yields_no_segment():
    segs = segment_answer("до\n```\n\n```\nпосле")
    assert [s.kind for s in segs] == ["prose", "prose"]


def test_fence_indented_inside_a_list_item_is_a_code_block():
    """Модель сдвигает блок кода под текст пункта списка; сырые кавычки в
    ленте — это он же, не распознанный (живая приёмка режима plan)."""
    from gc_tui.services.markdown_render import segment_answer

    text = (
        "1. Добавить импорт\n"
        "   В начало файла:\n"
        "   ```python\n"
        "   from datetime import datetime\n"
        "       x = 1\n"
        "   ```\n"
        "2. Дальше\n"
    )
    blocks = segment_answer(text)
    assert [b.kind for b in blocks] == ["prose", "code-block", "prose"]
    code = blocks[1]
    assert code.lang == "python"
    # отступ ограды снят, собственный отступ кода сохранён
    assert code.content == "from datetime import datetime\n    x = 1"


def test_deeply_indented_backticks_inside_a_block_do_not_close_it():
    """Закрывающая ограда — не глубже открывающей плюс три пробела
    (CommonMark). Иначе пример markdown с отступом внутри кода закрывал
    блок раньше времени, а настоящая ограда открывала новый до конца ответа."""
    from gc_tui.services.markdown_render import segment_answer

    text = "\n".join(
        [
            "```python",
            "def f():",
            '    """Пример:',
            "        ```",
            "        x",
            "        ```",
            '    """',
            "```",
            "после блока",
        ]
    )
    kinds = [b.kind for b in segment_answer(text)]
    assert kinds == ["code-block", "prose"]


def test_four_space_backticks_inside_a_docstring_do_not_close_the_block():
    """CommonMark: закрывающая ограда — не глубже открывающей плюс три пробела.
    Пример markdown с отступом 4 внутри функции блок не рвёт."""
    from gc_tui.services.markdown_render import segment_answer

    text = '```python\ndef f():\n    """Example:\n    ```\n    x = 1\n    ```\n    """\n    return 1\n```\nafter'
    assert [b.kind for b in segment_answer(text)] == ["code-block", "prose"]


def test_indented_fence_at_top_level_stays_prose_as_in_develop():
    """Отступной блок верхнего уровня с примером ограды — не ограждённый
    код: сдвинутые кавычки — ограда только внутри пункта списка (§10.5)."""
    text = "Пример:\n\n    ```bash\n    pip install\n    ```\n\nдальше"
    assert segment_answer(text) == [Block("prose", text)]


def test_slightly_indented_backticks_do_not_close_a_top_level_block():
    """Ограду верхнего уровня закрывает только ограда без отступа, как в
    develop: строка кода «  ```» — содержимое."""
    text = "```python\nx = '''\n  ```\n'''\n```\nпосле"
    segs = segment_answer(text)
    assert [s.kind for s in segs] == ["code-block", "prose"]
    assert segs[0].content == "x = '''\n  ```\n'''"


def test_indented_fence_after_a_blank_line_in_a_list_is_code():
    """Пустая строка внутри пункта список не закрывает."""
    text = "- шаг\n\n  ```sh\n  ls\n  ```\nконец"
    segs = segment_answer(text)
    assert [s.kind for s in segs] == ["prose", "code-block", "prose"]
    assert segs[1].content == "ls"
    assert segs[1].lang == "sh"


def test_prose_render_keeps_a_fence_inside_a_list_item_raw():
    """Проза стрима ведёт тот же контекст списка, что сегменты финала:
    строка кода внутри ограды пункта — как есть, не маркер «•»."""
    plain = render_prose("- шаг\n  ```sh\n  - x\n  ```").plain
    assert "  - x" in plain
    assert "• x" not in plain


def test_prose_render_keeps_a_top_level_indented_fence_as_prose():
    """На верхнем уровне сдвинутые кавычки ограду не открывают и в прозе."""
    plain = render_prose("Пример:\n\n    ```\n- пункт").plain
    assert "• пункт" in plain


def test_bold_and_heading_lines_are_ink_bold_in_css():
    """Строки-выделения прозы (`.line-bold`, заголовки) — `$ink` жирным,
    без акцента (решение владельца 2026-10-23)."""
    import re

    from gc_testkit.paths import TUI_PKG

    css = (TUI_PKG / "styles.tcss").read_text(encoding="utf-8")
    for cls in ("line-bold", "line-heading-1", "line-heading-2"):
        (rule,) = re.findall(rf"^\.{cls}\s*\{{([^}}]*)\}}", css, re.M)
        assert "color: $ink;" in rule
        assert "text-style: bold;" in rule
