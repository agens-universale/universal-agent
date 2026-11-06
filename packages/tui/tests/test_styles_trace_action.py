"""Ховер фраз ленты (дизайн-код §6): одно правило `.trace-action:hover` с
`$accent`; действия ленты (❐, «… ещё N строк») несут этот класс."""

import re

from gc_testkit.paths import TUI_PKG

_HOVER_ACCENT = re.compile(r"([^{}]*?):hover\s*\{[^}]*\$accent[^}]*\}")


def _hover_accent_rules() -> list[str]:
    css = (TUI_PKG / "styles.tcss").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [
        m.group(1).strip()
        for m in _HOVER_ACCENT.finditer(css)
        if "scrollbar" not in m.group(0)
    ]


def test_single_hover_rule_for_trace_phrases():
    assert _hover_accent_rules() == [".trace-action"]


def test_trace_actions_carry_the_class():
    """❐ и хвост свёртки создаются только с классом `trace-action`."""
    widgets = TUI_PKG / "widgets"
    for name, cls in (
        ("block_panel.py", "block-copy"),
        ("tool_call_row.py", "block-copy"),
        ("diff_block.py", "diff-more"),
    ):
        source = (widgets / name).read_text(encoding="utf-8")
        assert f'classes="{cls} trace-action"' in source, name
        assert f'classes="{cls}"' not in source, name


def test_scroll_end_pill_is_the_hover_exception():
    """Плашка «в конец» ховер красит фоном, а не `$accent`: класса нет."""
    main = (TUI_PKG / "screens" / "main.py").read_text(encoding="utf-8")
    assert "ScrollEndPill(trace" in main
    assert 'id="trace-end-pill", classes="trace-action"' not in main
