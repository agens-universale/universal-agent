"""Общий блик по слову с рампой параметром."""

from gc_tui.palette import DIM, INK, MCP_SHIMMER_SHADES
from gc_tui.services.shimmer import shimmer_pass, shimmer_text


def _colors(text):
    return {str(span.style) for span in text.spans}


def test_shimmer_with_mcp_ramp_rests_on_dim_and_peaks_ink():
    word = "подключаю MCP"
    assert _colors(shimmer_text(word, 0, MCP_SHIMMER_SHADES)) == {DIM}
    peak = shimmer_text(word, 3 + 3, MCP_SHIMMER_SHADES)  # центр на букве 3
    assert INK in _colors(peak)
    assert shimmer_pass(word, MCP_SHIMMER_SHADES) == len(word) + 6
