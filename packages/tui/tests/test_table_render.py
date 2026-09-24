"""table_render — markdown-таблица как rich.Text, пригодный для выделения мышью."""

from __future__ import annotations

from gc_tui.services.table_render import table_to_text
from rich.text import Text

HEADERS = ["Стратегия", "Принцип"]
ROWS = [["LRU", "вытесняет давние"], ["LFU", "вытесняет редкие"]]
ALIGNS = ["left", "left"]


def test_returns_text():
    assert isinstance(table_to_text(HEADERS, ROWS, ALIGNS, width=60), Text)


def test_contains_all_cells():
    plain = table_to_text(HEADERS, ROWS, ALIGNS, width=60).plain
    for value in ("Стратегия", "Принцип", "LRU", "LFU", "вытесняет давние"):
        assert value in plain


def test_has_no_trailing_newline():
    assert not table_to_text(HEADERS, ROWS, ALIGNS, width=60).plain.endswith(
        "\n"
    )


def test_narrow_width_produces_more_lines():
    narrow = table_to_text(HEADERS, ROWS, ALIGNS, width=24).plain.count("\n")
    wide = table_to_text(HEADERS, ROWS, ALIGNS, width=80).plain.count("\n")
    assert narrow > wide


def test_lines_fit_requested_width():
    for line in table_to_text(HEADERS, ROWS, ALIGNS, width=40).plain.split(
        "\n"
    ):
        assert len(line) <= 40


def test_keeps_styles():
    assert table_to_text(HEADERS, ROWS, ALIGNS, width=60).spans
