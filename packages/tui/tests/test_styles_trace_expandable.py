"""Ховер раскрываемого текста (дизайн-код §6): одно правило
`.trace-expandable:hover` с `$ink`; текст, раскрываемый кликом по нему
самому (строка мысли), несёт этот класс, `− свернуть` его тела —
`trace-action`."""

import re

from gc_testkit.paths import TUI_PKG

_HOVER_INK = re.compile(r"([^{}]*?):hover\s*\{[^}]*\$ink[^}]*\}")


def _hover_ink_rules() -> list[str]:
    css = (TUI_PKG / "styles.tcss").read_text(encoding="utf-8")
    css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
    return [m.group(1).strip() for m in _HOVER_INK.finditer(css)]


def test_single_hover_rule_for_expandable_text():
    assert _hover_ink_rules() == [".trace-expandable"]


def test_expandable_widgets_carry_the_classes():
    widgets = TUI_PKG / "widgets"
    row = (widgets / "thought_row.py").read_text(encoding="utf-8")
    body = (widgets / "expandable_text.py").read_text(encoding="utf-8")
    call = (widgets / "tool_call_row.py").read_text(encoding="utf-8")
    assert '"trace-expandable"' in row
    assert '"trace-expandable"' in call
    assert 'classes="expandable-collapse trace-action"' in body
