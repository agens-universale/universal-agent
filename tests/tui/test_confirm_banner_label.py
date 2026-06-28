"""Метка-заголовок confirm/guard баннера — глиф шлюза вместо '?'."""
from tui.widgets.confirm_banner import GUARD_GLYPH, build_guard_label
from tui.palette import RUN_COLOR


def test_guard_label_uses_caution_glyph_not_question():
    label = build_guard_label("Защита записи")
    assert label.plain == f"{GUARD_GLYPH} Защита записи"
    assert "?" not in label.plain


def test_guard_glyph_is_amber():
    label = build_guard_label("Защита чтения")
    # первый span красит глиф в run-color (янтарь)
    first = label.spans[0]
    assert first.start == 0
    assert str(first.style) == RUN_COLOR
