"""render-A: парсит markdown, удаляет синтаксис, возвращает spans (text, class)."""
from tui.palette import CODE
from tui.services.markdown_render import (
    Block,
    render_block,
    render_block_to_text,
)


_CODE_HEX = CODE[1:].lower()  # '#XXXXXX' → 'xxxxxx' для substring-проверок в span.style


def test_heading_h1_strips_hashes():
    blocks = render_block("## Заголовок\nтело")
    assert blocks[0].kind == "heading-1"
    assert "Заголовок" in blocks[0].content
    assert "##" not in blocks[0].content


def test_heading_h3_uses_heading_2_class():
    blocks = render_block("### Подзаголовок\ntext")
    assert blocks[0].kind == "heading-2"


def test_bullet_list_neutral():
    blocks = render_block("- первый\n- второй")
    kinds = [b.kind for b in blocks]
    assert kinds.count("bullet") == 2


def test_code_block_is_neutral():
    blocks = render_block("```\ncode here\n```")
    kinds = [b.kind for b in blocks]
    assert "code-block" in kinds


def test_render_block_fenced_with_lang():
    blocks = render_block("```python\nx = 1\ny = 2\n```")
    assert blocks == [Block(kind="code-block", content="x = 1\ny = 2", lang="python")]


def test_render_block_fenced_no_lang():
    blocks = render_block("```\nx = 1\n```")
    assert blocks == [Block(kind="code-block", content="x = 1", lang=None)]


def test_render_block_mixed_prose_and_code():
    text = "Вот пример:\n```js\nconst x = 1;\n```\nКонец."
    blocks = render_block(text)
    assert blocks == [
        Block(kind="para", content="Вот пример:", lang=None),
        Block(kind="code-block", content="const x = 1;", lang="js"),
        Block(kind="para", content="Конец.", lang=None),
    ]


def test_render_block_heading_and_bullet_have_no_lang():
    blocks = render_block("# Title\n- item")
    assert blocks == [
        Block(kind="heading-1", content="Title", lang=None),
        Block(kind="bullet", content="item", lang=None),
    ]


# ----------------------------------------------------------------------------
# render_block_to_text — Block → rich.text.Text (canon §3.2)
# ----------------------------------------------------------------------------

def test_render_block_to_text_heading_is_accent_bold():
    text = render_block_to_text(Block(kind="heading-1", content="Заголовок"))
    assert text.plain == "Заголовок"
    accent_bold_spans = [
        s for s in text.spans
        if "54b3a6" in str(s.style).lower() and "bold" in str(s.style).lower()
    ]
    assert any("Заголовок" in text.plain[s.start:s.end] for s in accent_bold_spans)


def test_render_block_to_text_bullet_has_marker_prefix():
    text = render_block_to_text(Block(kind="bullet", content="первый"))
    assert text.plain == "• первый"


def test_render_block_to_text_para_applies_inline_styles():
    text = render_block_to_text(Block(kind="para", content="есть **bold** и `code`"))
    plain = text.plain
    assert "**" not in plain
    assert "`" not in plain
    assert "bold" in plain
    assert "code" in plain
    accent_spans = [s for s in text.spans if "54b3a6" in str(s.style).lower()]
    assert any("bold" in text.plain[s.start:s.end] for s in accent_spans)
    code_spans = [s for s in text.spans if _CODE_HEX in str(s.style).lower()]
    assert any("code" in text.plain[s.start:s.end] for s in code_spans)


def test_render_block_table_basic():
    blocks = render_block("| A | B |\n|---|---|\n| 1 | 2 |\n| 3 | 4 |")
    assert len(blocks) == 1
    b = blocks[0]
    assert b.kind == "table"
    assert b.headers == ["A", "B"]
    assert b.rows == [["1", "2"], ["3", "4"]]
    assert b.aligns == ["left", "left"]


def test_render_block_table_alignment_markers():
    blocks = render_block("| L | C | R |\n|:--|:-:|--:|\n| a | b | c |")
    assert blocks[0].aligns == ["left", "center", "right"]


def test_render_block_pipe_line_without_separator_is_para():
    blocks = render_block("a | b | c\nплейн текст")
    assert all(b.kind != "table" for b in blocks)
    assert blocks[0].kind == "para"


def test_render_block_table_ragged_row_padded():
    blocks = render_block("| A | B | C |\n|---|---|---|\n| 1 | 2 |")
    assert blocks[0].rows == [["1", "2", ""]]


# ----------------------------------------------------------------------------
# build_markdown_table — rich.Table (полная сетка, dim-рамка, accent-заголовок)
# ----------------------------------------------------------------------------

def test_build_markdown_table_columns_and_justify():
    from tui.services.markdown_render import build_markdown_table
    t = build_markdown_table(["A", "B"], [["1", "2"]], ["left", "right"])
    assert len(t.columns) == 2
    assert t.columns[1].justify == "right"


def test_build_markdown_table_header_accent_bold():
    from tui.palette import ACCENT
    from tui.services.markdown_render import build_markdown_table
    t = build_markdown_table(["Заголовок"], [["x"]], ["left"])
    hs = str(t.columns[0].header_style).lower()
    assert ACCENT[1:].lower() in hs and "bold" in hs


def test_build_markdown_table_cell_inline_code_stripped():
    from rich.text import Text
    from tui.services.markdown_render import build_markdown_table
    t = build_markdown_table(["H"], [["`x`"]], ["left"])
    cell = t.columns[0]._cells[0]
    assert isinstance(cell, Text)
    assert cell.plain == "x"  # бэктики срезаны → inline отработал
