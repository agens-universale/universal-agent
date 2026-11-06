"""Блик по слову индикатора: светлое пятно бежит по буквам слева направо,
стартуя во вспышку глифа."""

from gc_tui.palette import GLOW_SHADES, RUN_COLOR, SHIMMER_SHADES
from gc_tui.widgets.thinking_indicator import (
    ThinkingIndicator,
    shimmer_pass,
    shimmer_text,
)

_SPREAD = len(SHIMMER_SHADES) - 1
_GLOW_PEAK = len(GLOW_SHADES) // 2


def _colors(text):
    by_char = [None] * len(text.plain)
    for span in text.spans:
        for i in range(span.start, span.end):
            by_char[i] = str(span.style)
    return by_char


def test_shimmer_shades_climb_from_run_color_to_glow_peak():
    """Блик только светлее основного амбера — та же семья, что значок."""
    assert SHIMMER_SHADES[0] == RUN_COLOR
    assert SHIMMER_SHADES[-1] == GLOW_SHADES[_GLOW_PEAK]


def test_step_zero_and_after_pass_are_plain():
    """Вне прохода — слово ровным амбером (и снапшоты с замороженным тиком)."""
    word = "думаю…"
    assert set(_colors(shimmer_text(word, 0))) == {RUN_COLOR}
    after = shimmer_pass(word) + 1
    assert set(_colors(shimmer_text(word, after))) == {RUN_COLOR}


def test_band_peaks_on_its_letter_and_fades_to_the_sides():
    step = _SPREAD + 2  # центр блика на букве 2
    colors = _colors(shimmer_text("думаю…", step))
    assert colors[2] == SHIMMER_SHADES[-1]
    assert colors[1] == colors[3] == SHIMMER_SHADES[-2]
    assert colors[0] == colors[4] == SHIMMER_SHADES[-3]
    assert colors[5] == RUN_COLOR


def test_band_moves_one_letter_per_step():
    word = "думаю…"
    first = _colors(shimmer_text(word, _SPREAD + 1)).index(SHIMMER_SHADES[-1])
    second = _colors(shimmer_text(word, _SPREAD + 2)).index(SHIMMER_SHADES[-1])
    assert second == first + 1


def test_shimmer_keeps_the_word_text():
    assert shimmer_text("копаю глубже…", 7).plain == "копаю глубже…"


def test_pass_starts_at_glyph_flash():
    w = ThinkingIndicator()
    w._frame = _GLOW_PEAK - 2
    w._tick()
    assert w._shimmer_step is None  # глиф ещё не вспыхнул
    w._tick()
    assert w._frame % len(GLOW_SHADES) == _GLOW_PEAK
    assert w._shimmer_step == 1


def test_finished_pass_waits_for_next_glyph_flash():
    w = ThinkingIndicator()
    w._frame = _GLOW_PEAK
    w._shimmer_step = shimmer_pass("думаю…")
    w._tick()
    assert w._shimmer_step is None
    while w._frame % len(GLOW_SHADES) != _GLOW_PEAK - 1:
        w._tick()
        assert w._shimmer_step is None
    w._tick()
    assert w._shimmer_step == 1


def test_thinking_word_shimmers():
    w = ThinkingIndicator()
    w._shimmer_step = _SPREAD  # центр блика на первой букве
    t = w._render_text()
    word_at = t.plain.index("думаю")
    assert _colors(t)[word_at] == SHIMMER_SHADES[-1]


def test_compressing_word_shimmers_too():
    w = ThinkingIndicator(state="compressing")
    w._shimmer_step = _SPREAD
    t = w._render_text()
    word_at = t.plain.index("сжимаю")
    assert _colors(t)[word_at] == SHIMMER_SHADES[-1]


def test_no_shimmer_in_grey_states():
    w = ThinkingIndicator(state="processing")
    w._frame = _GLOW_PEAK - 1
    w._tick()
    assert w._shimmer_step is None
