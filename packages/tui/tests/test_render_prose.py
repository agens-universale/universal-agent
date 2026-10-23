from gc_tui.palette import ACCENT, CODE, INK
from gc_tui.services.markdown_render import render_prose


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


def _style_of(t, part: str) -> str:
    i = t.plain.index(part)
    (span,) = [s for s in t.spans if s.start <= i < s.end]
    return str(span.style)


def test_heading_is_ink_bold_without_accent():
    """Заголовок — цветом строки (`INK`) жирным; акцента нет (решение
    владельца 2026-10-23: всё выделение — `INK` жирный)."""
    t = render_prose("# Заголовок")
    assert t.plain == "Заголовок"
    assert _style_of(t, "Заголовок") == f"{INK} bold"


def test_para_bold_ink_and_code_styled_spans():
    t = render_prose("есть **bold** и `code`")
    assert "**" not in t.plain and "`" not in t.plain
    assert _style_of(t, "bold") == f"{INK} bold"
    assert ACCENT.lower() not in str(t.spans).lower()
    assert _style_of(t, "code") == CODE


def test_bold_without_own_colour_when_css_owns_line():
    """`ink=""` — цвет задаёт CSS строки: жирный без своего цвета."""
    t = render_prose("**жирно**", ink="")
    assert _style_of(t, "жирно") == "bold"


def test_prose_fence_state_ignores_inner_fence_with_info_string():
    text = "```markdown\n# T\n```bash\nx\n```\n## после\n"
    out = render_prose(text).plain.split("\n")
    # «## после» — уже проза: блок закрыл голый ```, а не ```bash
    assert out[-2] == "после"
