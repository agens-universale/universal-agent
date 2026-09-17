"""Чистый рендер голосовой пилюли: ширина, таймеры, REC+эквалайзер, волна.

Визуал зафиксирован единый (живая приёмка 2026-07-05): запись — «● REC» +
спектр-эквалайзер braille-рампой, распознавание — braille-волна расщепления.
"""
from gc_tui.services.voice_pill import (
    PILL_WIDTH, _RAMP, format_rec_time, format_stt_time, render_pill,
    _transcribing_levels,
)


def test_pill_is_exactly_pill_width_cells():
    assert len(render_pill("recording", 7.0, frame=0).plain) == PILL_WIDTH
    assert len(render_pill("transcribing", 0.4, frame=3).plain) == PILL_WIDTH


def test_recording_blinking_dot_and_label():
    on = render_pill("recording", 7.0, frame=0).plain
    off = render_pill("recording", 7.0, frame=3).plain
    assert "REC" in on and "0:07" in on
    assert "●" in on and "●" not in off   # кругляшок мигает


def test_recording_spectrum_equalizer():
    def pill(bands):
        return render_pill("recording", 1.0, frame=0, bands=bands).plain

    def glyphs_of(s):
        return [ch for ch in s if ch in "⡀⣀⣤⣶⣿"]

    assert glyphs_of(pill((0.0,) * 7)) == []            # тишина — эквалайзер пуст
    assert glyphs_of(pill((0.002,) * 7)) == []          # шумовой пол — гейт
    assert len(glyphs_of(pill((0.5,) * 7))) == 7        # все полосы живы
    # прыгает только «своя» колонка
    solo = pill((0.0, 0.0, 0.5, 0.0, 0.0, 0.0, 0.0))
    assert len(glyphs_of(solo)) == 1
    # высота колонки монотонна по уровню полосы (дБ-шкала)
    height = lambda s: max((_RAMP.index(ch) for ch in glyphs_of(s)), default=0)
    assert height(pill((0.01,) + (0.0,) * 6)) < height(pill((0.3,) + (0.0,) * 6))


def test_transcribing_braille_wave_flies():
    a = render_pill("transcribing", 0.4, frame=1).plain
    b = render_pill("transcribing", 0.4, frame=5).plain
    assert any(ch in "⡀⣀⣤⣶⣿" for ch in a)
    assert a != b                          # волна летит
    assert "0,4с" in a
    # зазор-воздух: ячейка слева от таймера всегда пустая
    assert a[a.index("0,4с") - 1] == " "


def test_transcribing_wave_moves_every_frame():
    """Ни одного «стоячего» кадра: раньше позиция округлялась до целой ячейки, на
    8 положений приходилось 16 кадров, и волна перескакивала раз в треть секунды
    (кадры 0 и 1 были посимвольно одинаковы). Дробная позиция это убирает."""
    from gc_tui.services.voice_pill import _CYCLE_FRAMES, _transcribing_levels
    frames = [tuple(_transcribing_levels(f)) for f in range(_CYCLE_FRAMES)]
    stuck = [f for f in range(1, len(frames)) if frames[f] == frames[f - 1]]
    assert stuck == [], f"кадры без изменений: {stuck}"


def test_transcribing_wave_has_no_holes():
    """Тело волны сплошное: порог гашения не должен выедать дырки в её середине —
    рваная волна читается как артефакт, а не как затухание."""
    from gc_tui.services.voice_pill import _CYCLE_FRAMES, _transcribing_levels
    for f in range(_CYCLE_FRAMES):
        lit = [i for i, lvl in enumerate(_transcribing_levels(f)) if lvl > 0]
        # разрывы допустимы только между копиями (их две пары), не внутри тела
        gaps = [b - a for a, b in zip(lit, lit[1:]) if b - a > 1]
        assert len(gaps) <= 4, f"кадр {f}: слишком рваная лента ({gaps})"


def test_recording_timer_format():
    assert format_rec_time(7.2) == "0:07"
    assert format_rec_time(65.9) == "1:05"
    assert "0:07" in render_pill("recording", 7.2, frame=0).plain


def test_transcribing_timer_tenths_with_comma():
    assert format_stt_time(0.42) == "0,4с"
    assert format_stt_time(1.0) == "1,0с"
    assert "0,4с" in render_pill("transcribing", 0.42, frame=0).plain


def test_transcribing_wave_travels_toward_edges():
    seen_left = set()
    for f in range(16):
        lv = _transcribing_levels(f)
        seen_left.update(i for i, v in enumerate(lv[:8]) if v > 0)
    assert min(seen_left) <= 3   # копии волны доходят почти до левого края


def test_gradient_first_and_last_cells_differ():
    text = render_pill("recording", 0.0, frame=0)
    styles = [span.style for span in text.spans]
    assert str(styles[0]) != str(styles[-1])   # лента, а не однотонная заливка
