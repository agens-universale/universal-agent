"""Реестр текстов слота футера: форма справок и влезание в ширину."""

from __future__ import annotations

from gc_tui.services import footer_slot
from gc_tui.services.footer_slot import (
    MAIN_FOOTER_MIN_COLS,
    TRUNCATABLE,
    toggle_info_line,
)
from rich.cells import cell_len


def _slot_texts() -> list[str]:
    texts: list[str] = []
    for name, value in vars(footer_slot).items():
        if name.startswith("_") or not name.isupper():
            continue
        if isinstance(value, str):
            texts.append(value)
        elif isinstance(value, dict):
            texts.extend(v for v in value.values() if isinstance(v, str))
        elif isinstance(value, tuple):
            texts.extend(v for v in value if isinstance(v, str))
    return texts


def test_toggle_info_line_format():
    assert (
        toggle_info_line("gate", "full")
        == "/gate: full · ←/→ в меню команд — изменить"
    )


def test_no_text_promises_enter_toggle():
    assert all("Enter — переключить" not in t for t in _slot_texts())


def test_texts_fit_beside_right_column():
    limit = MAIN_FOOTER_MIN_COLS - 20
    too_long = [
        t
        for t in _slot_texts()
        if t not in TRUNCATABLE and cell_len(t) > limit
    ]
    assert too_long == []


def test_toggle_info_line_is_one_line():
    # динамический текст: при нехватке ширины режется многоточием
    assert "\n" not in toggle_info_line("answers", "judge")


def test_min_cols():
    assert MAIN_FOOTER_MIN_COLS == 64
