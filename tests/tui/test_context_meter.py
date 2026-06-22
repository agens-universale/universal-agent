"""render_context_meter: текст «контекст <bar> NN%» + цвет порогами."""
import pytest

from tui.services.context_meter import render_context_meter
from tui.palette import OK, RUN_COLOR, WARN


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
    text, col = render_context_meter(chars, threshold)
    assert f"{pct}%" in text
    assert text.startswith("контекст ")
    assert col == color


def test_bar_is_eight_cells():
    text, _ = render_context_meter(500, 1000)  # 50%
    bar = text.split(" ")[1]
    assert len(bar) == 8
    assert set(bar) <= {"▓", "░"}
