"""Пилюля режима разрешений: глиф, имя, подсказка хоткея, ширина."""

from gc_core.security.permission_mode import AVAILABLE_MODES, mode_label
from gc_tui.palette import AUTO_MODE, DEEP_DIM, INK, RUN_COLOR
from gc_tui.services.mode_pill import MODE_HINTS, render_mode_pill

_MODES = [mode.value for mode in AVAILABLE_MODES]


def test_pill_shows_glyph_and_name():
    assert render_mode_pill("edit").plain == "▸▸ edit mode (Shift+Tab)"
    assert render_mode_pill("manual").plain == "⏸ manual mode (Shift+Tab)"


def test_every_available_mode_has_a_unique_glyph_a_name_and_a_hint():
    """Сторож полноты: собранный режим без глифа, имени или подсказки."""
    glyphs = {render_mode_pill(mode).plain.split()[0] for mode in _MODES}
    assert len(glyphs) == len(_MODES)
    for mode in _MODES:
        assert mode_label(mode) in render_mode_pill(mode).plain
        assert MODE_HINTS[mode]
        assert mode not in MODE_HINTS[mode]  # подсказка — смысл, не имя режима


def test_pill_colors_by_mode():
    assert INK in str(render_mode_pill("edit").spans[0].style)
    assert DEEP_DIM in str(render_mode_pill("manual").spans[0].style)


def test_auto_mode_is_lighter_amber_than_frames():
    """Подпись авторежима — свой светлый амбер, рамки остаются RUN_COLOR."""
    style = str(render_mode_pill("auto").spans[0].style)
    assert AUTO_MODE in style
    assert AUTO_MODE != RUN_COLOR


def test_pill_width_follows_its_content():
    """Пилюля не добивается пробелами про запас."""
    for mode in _MODES:
        for hint in (True, False):
            pill = render_mode_pill(mode, hint=hint)
            assert pill.plain == pill.plain.rstrip()


def test_narrow_pill_is_glyph_only():
    narrow = render_mode_pill("edit", narrow=True)
    assert narrow.plain == "▸▸"


def test_hotkey_hint_touches_the_mode_name_and_can_be_suppressed():
    for mode in _MODES:
        assert (
            f"{mode_label(mode)} (Shift+Tab)" in render_mode_pill(mode).plain
        )
        assert "Shift+Tab" not in render_mode_pill(mode, hint=False).plain


def test_unknown_mode_falls_back_to_the_default_one():
    assert (
        render_mode_pill("нет такого").plain == render_mode_pill("edit").plain
    )
