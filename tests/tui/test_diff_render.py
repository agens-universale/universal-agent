"""Подсветка unified diff: + зелёный, − красный, заголовки приглушённые."""
from __future__ import annotations

import pytest

from tui.palette import CODE_BLOCK, ERR, MUTED, OK


def test_style_unified_diff_colors_plus_minus_and_headers():
    pytest.importorskip("rich")
    from tui.services.diff_render import style_unified_diff

    raw = (
        "--- a/foo\n"
        "+++ b/foo\n"
        "@@ -1,2 +1,2 @@\n"
        " context\n"
        "-old\n"
        "+new\n"
    )
    styled = style_unified_diff(raw)
    assert styled.plain == raw

    spans_by_text = {}
    for start, end, style in styled.spans:
        spans_by_text[styled.plain[start:end].rstrip("\n")] = str(style)

    assert spans_by_text["--- a/foo"] == MUTED
    assert spans_by_text["+++ b/foo"] == MUTED
    assert spans_by_text["@@ -1,2 +1,2 @@"] == MUTED
    assert spans_by_text[" context"] == CODE_BLOCK
    assert spans_by_text["-old"] == ERR
    assert spans_by_text["+new"] == OK


def test_style_unified_diff_does_not_treat_file_headers_as_add_del():
    pytest.importorskip("rich")
    from tui.services.diff_render import style_unified_diff

    styled = style_unified_diff("--- a\n+++ b\n+added\n-removed\n")
    styles = [str(style) for _, _, style in styled.spans]
    assert MUTED in styles
    assert OK in styles
    assert ERR in styles


def test_render_unified_diff_widget_returns_static():
    pytest.importorskip("textual")
    from tui.services.diff_render import render_unified_diff_widget

    w = render_unified_diff_widget("+ok\n-bad\n")
    assert w is not None
    assert "+ok" in w.content.plain
    assert "-bad" in w.content.plain
