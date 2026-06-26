from tui.services.markdown_render import render_prose


def test_plain_prose_kept():
    assert render_prose("просто текст").plain == "просто текст"


def test_bold_markers_stripped():
    t = render_prose("это **жирно**")
    assert "жирно" in t.plain and "**" not in t.plain


def test_inline_code_backticks_stripped():
    t = render_prose("вот `код`")
    assert "код" in t.plain and "`" not in t.plain


def test_heading_hashes_stripped():
    assert render_prose("# Заголовок").plain == "Заголовок"


def test_bullet_marker_normalized():
    assert render_prose("- пункт").plain.startswith("• пункт")


def test_fenced_lines_stay_plain():
    t = render_prose("```python\nx = 1\n```")
    assert "x = 1" in t.plain and "```python" in t.plain


def test_empty_text_empty():
    assert render_prose("").plain == ""


def test_heading_is_accent_bold():
    t = render_prose("# Заголовок")
    assert t.plain == "Заголовок"
    assert any("54b3a6" in str(s.style).lower() and "bold" in str(s.style).lower()
               for s in t.spans)


def test_para_bold_and_code_styled_spans():
    t = render_prose("есть **bold** и `code`")
    assert "**" not in t.plain and "`" not in t.plain
    assert any("54b3a6" in str(s.style).lower() for s in t.spans)   # accent (bold)
    assert any("afbdff" in str(s.style).lower() for s in t.spans)   # code (slate)
