"""render_context_meter: Rich-Text «контекст <bar> NN%»; заполнено/пусто — цветом."""
import pytest

from gc_tui.services.context_meter import render_context_meter, _TRACK
from gc_tui.palette import OK, RUN_COLOR, WARN


def test_hidden_when_threshold_zero():
    assert render_context_meter(100, 0) is None


@pytest.mark.parametrize("chars,threshold,pct,color", [
    (0, 1000, 0, OK),
    (590, 1000, 59, OK),
    (600, 1000, 60, RUN_COLOR),
    (900, 1000, 90, RUN_COLOR),
    (910, 1000, 91, WARN),
    (1000, 1000, 100, WARN),
    (5000, 1000, 100, WARN),   # over threshold → cap 100%
])
def test_pct_and_color(chars, threshold, pct, color):
    m = render_context_meter(chars, threshold)
    assert f"{pct}%" in m.plain
    assert m.plain.startswith("контекст ")
    # порог задаёт цвет заполненной части / метки (присутствует среди спанов)
    assert any(str(s.style) == color for s in m.spans)


def test_bar_is_eight_full_blocks():
    m = render_context_meter(500, 1000)  # 50%
    bar = m.plain.split(" ")[1]
    assert len(bar) == 8
    assert set(bar) == {"█"}             # один надёжный глиф, без shade-символов


def test_filled_and_empty_distinguished_by_color_not_shade():
    """25% → 2 заполненных (цвет порога) + 6 пустых (трек); shade-глифов нет."""
    m = render_context_meter(250, 1000)
    styles = {str(s.style) for s in m.spans}
    assert OK in styles                  # заполненная часть — цвет порога
    assert _TRACK in styles              # пустая часть — тусклый трек, не «чёрная»
    assert "░" not in m.plain and "▓" not in m.plain   # ушли от font-зависимых shade
