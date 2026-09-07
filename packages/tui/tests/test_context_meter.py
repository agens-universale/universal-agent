"""render_context_meter: фикс-ширины реверс-градиент плашка «контекст NN%» (pill-стиль)."""
import pytest

from gc_tui.services.context_meter import render_context_meter, _TRACK, _WIDTH
from gc_tui.branding import gradient_color


def _bg_hex(span):
    """Hex фона ячейки-спана (#rrggbb, lower) или None."""
    bg = span.style.bgcolor
    return bg.get_truecolor().hex if bg else None


def _fg_hex(span):
    fg = span.style.color
    return fg.get_truecolor().hex if fg else None


def test_hidden_when_threshold_zero():
    assert render_context_meter(100, 0) is None


def test_width_is_fixed():
    m = render_context_meter(500, 1000)
    assert len(m.plain) == _WIDTH
    assert len(m.spans) == _WIDTH          # ячейка = спан (append по одной)


def test_label_and_percent_present():
    m = render_context_meter(580, 1000)    # 58%
    assert "контекст" in m.plain
    assert "58%" in m.plain


@pytest.mark.parametrize("chars,threshold,pct", [
    (0, 1000, 0),
    (200, 1000, 20),
    (580, 1000, 58),
    (1000, 1000, 100),
    (5000, 1000, 100),                     # сверх порога → кап 100
])
def test_filled_count_and_track(chars, threshold, pct):
    m = render_context_meter(chars, threshold)
    assert f"{pct}%" in m.plain
    filled = round(_WIDTH * pct / 100)
    for i in range(_WIDTH):
        bg = _bg_hex(m.spans[i])
        if i < filled:
            assert bg is not None and bg != _TRACK        # градиент
        else:
            assert bg == _TRACK                            # тусклый трек


def test_reverse_gradient_direction():
    """Полная плашка: левый край = ACCENT(teal), правый = RUN_COLOR(amber)."""
    m = render_context_meter(1000, 1000)   # 100% → все залиты
    assert _bg_hex(m.spans[0]) == gradient_color(1.0).lower()          # i=0 → 1-0 → teal
    assert _bg_hex(m.spans[_WIDTH - 1]) == gradient_color(0.0).lower() # i=W-1 → 0 → amber


def test_amber_hidden_until_full():
    """Низкий %: правый (amber) край ещё под треком — градиент «нагревается» по мере роста."""
    m = render_context_meter(200, 1000)    # 20% → filled=3
    assert _bg_hex(m.spans[_WIDTH - 1]) == _TRACK


def test_text_is_white():
    m = render_context_meter(580, 1000)
    idx = m.plain.index("к")               # первая буква метки «контекст»
    assert _fg_hex(m.spans[idx]) == "#ffffff"


def test_percent_bold_only_above_90():
    def pct_spans(m, pctstr):
        idx = m.plain.index(pctstr)
        return [m.spans[i] for i in range(idx, idx + len(pctstr))]
    hi = render_context_meter(950, 1000)   # 95% > 90 → bold
    lo = render_context_meter(580, 1000)   # 58% → не bold
    assert all(s.style.bold for s in pct_spans(hi, "95%"))
    assert not any(bool(s.style.bold) for s in pct_spans(lo, "58%"))
