from tui.services.markdown_render import render_stream_answer


def test_plain_prose_kept():
    assert render_stream_answer("просто текст").plain == "просто текст"


def test_bold_markers_stripped():
    t = render_stream_answer("это **жирно**")
    assert "жирно" in t.plain and "**" not in t.plain


def test_inline_code_backticks_stripped():
    t = render_stream_answer("вот `код`")
    assert "код" in t.plain and "`" not in t.plain


def test_heading_hashes_stripped():
    assert render_stream_answer("# Заголовок").plain == "Заголовок"


def test_bullet_marker_normalized():
    assert render_stream_answer("- пункт").plain.startswith("• пункт")


def test_fenced_lines_stay_plain():
    t = render_stream_answer("```python\nx = 1\n```")
    assert "x = 1" in t.plain and "```python" in t.plain


def test_empty_text_empty():
    assert render_stream_answer("").plain == ""
