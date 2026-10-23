"""Реестр текстов слота футера: форма справок и влезание в ширину."""

from __future__ import annotations

from footer_text import slot_texts
from gc_tui.services.footer_slot import (
    MAIN_FOOTER_MIN_COLS,
    TRUNCATABLE,
    toggle_info_line,
)
from rich.cells import cell_len


def test_toggle_info_line_format():
    assert (
        toggle_info_line("gate", "full")
        == "/gate: full · ←/→ в меню команд — изменить"
    )


def test_no_text_promises_enter_toggle():
    assert all("Enter — переключить" not in t for t in slot_texts())


def test_texts_fit_beside_right_column():
    limit = MAIN_FOOTER_MIN_COLS - 20
    too_long = [
        t for t in slot_texts() if t not in TRUNCATABLE and cell_len(t) > limit
    ]
    assert too_long == []


def test_toggle_info_line_is_one_line():
    # динамический текст: при нехватке ширины режется многоточием
    assert "\n" not in toggle_info_line("answers", "judge")


def test_min_cols():
    assert MAIN_FOOTER_MIN_COLS == 64
