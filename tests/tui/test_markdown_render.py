"""segment_answer: сегментация ответа на прозовые прогоны + code/table-блоки."""
from tui.services.markdown_render import Block, segment_answer, build_markdown_table


def test_pure_prose_is_one_run():
    assert segment_answer("просто текст") == [Block("prose", "просто текст")]


def test_blank_lines_preserved_inside_run():
    assert segment_answer("абзац1\n\nабзац2") == [Block("prose", "абзац1\n\nабзац2")]


def test_heading_and_bullet_stay_in_prose_run():
    # синтаксис НЕ срезаем — render_prose отрисует # и - построчно
    assert segment_answer("# Title\n- item") == [Block("prose", "# Title\n- item")]


def test_edge_blank_lines_trimmed():
    assert segment_answer("\n\nтекст\n\n") == [Block("prose", "текст")]


def test_fenced_with_lang_is_code_block():
    assert segment_answer("```python\nx = 1\ny = 2\n```") == [
        Block("code-block", "x = 1\ny = 2", "python")]


def test_fenced_no_lang():
    assert segment_answer("```\nx = 1\n```") == [Block("code-block", "x = 1", None)]


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


def test_table_alignment_markers():
    segs = segment_answer("| L | C | R |\n|:--|:-:|--:|\n| a | b | c |")
    assert segs[0].aligns == ["left", "center", "right"]


def test_pipe_line_without_separator_is_prose():
    assert segment_answer("a | b | c\nплейн текст") == [Block("prose", "a | b | c\nплейн текст")]


def test_table_ragged_row_padded():
    segs = segment_answer("| A | B | C |\n|---|---|---|\n| 1 | 2 |")
    assert segs[0].rows == [["1", "2", ""]]


def test_build_markdown_table_smoke():
    t = build_markdown_table(["A", "B"], [["1", "2"]], ["left", "left"])
    assert t.columns[0].header == "A"
