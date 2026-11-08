from gc_tui.palette import (
    ACCENT,
    ACCENT_BREATH_SHADES,
    ERR,
    NOTE_INK,
)
from gc_tui.services.start_status import (
    failed_row,
    loading_line,
    settled_row,
)


def _style_at(text, index):
    for span in text.spans:
        if span.start <= index < span.end:
            return str(span.style)
    return None


def test_loading_line_plain():
    line = loading_line(3, 9, ("filesystem", True), 0)
    assert line.plain == "• подключаю MCP 3/9 · filesystem ✓"
    assert loading_line(0, 9, None, 0).plain == "• подключаю MCP 0/9"


def test_loading_line_glyph_breathes():
    assert _style_at(loading_line(0, 9, None, 5), 0) == ACCENT
    first = ACCENT_BREATH_SHADES[0]
    assert _style_at(loading_line(0, 9, None, 0), 0) == first


def test_loading_line_failed_server_red_cross():
    line = loading_line(3, 9, ("fs", False), 0)
    assert _style_at(line, len(line.plain) - 1) == ERR


def test_loading_line_does_not_wrap():
    line = loading_line(3, 9, None, 0)
    assert line.no_wrap is True
    assert line.overflow == "ellipsis"


def test_settled_row_plain():
    assert settled_row(9, 9).plain == "✓ готов к работе   MCP 9/9 · /mcp"
    assert settled_row(7, 9).plain == "✓ готов к работе   ⚠ MCP 7/9 · /mcp"
    assert settled_row(0, 0).plain == "✓ готов к работе"


def test_failed_row_plain():
    assert failed_row(2).plain == "⚠ MCP: загрузка не завершилась · /mcp"


def test_failed_row_without_mcp_servers_plain():
    row = failed_row(0)
    assert row.plain == "⚠ агент не готов — причина в ленте ниже"
    assert "MCP" not in row.plain
    assert "/mcp" not in row.plain


def test_server_name_is_note_ink():
    line = loading_line(3, 9, ("filesystem", True), 0)
    assert _style_at(line, line.plain.index("filesystem")) == NOTE_INK
