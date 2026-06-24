import pytest
from rich.cells import cell_len
from rich.text import Text

from tui.widgets.thinking_indicator import ThinkingIndicator


def test_cancelled_phrase_no_badge_when_unmounted():
    ind = ThinkingIndicator(state="cancelled", clock=lambda: 0.0)
    assert ind._render_text().plain == "■ Отменено"


def test_failed_phrase():
    ind = ThinkingIndicator(state="failed", clock=lambda: 0.0)
    assert ind._render_text().plain == "✕ Ошибка"


def test_done_uses_frozen_elapsed():
    ind = ThinkingIndicator(state="done", frozen_elapsed=12, clock=lambda: 999.0)
    assert ind._render_text().plain == "✓ Выполнил за 12с"


def test_done_without_elapsed_shows_bare():
    # Восстановление старой сессии без сохранённого elapsed → «✓ Выполнил» без «за N».
    ind = ThinkingIndicator(state="done", frozen_elapsed=None, clock=lambda: 0.0)
    assert ind._render_text().plain == "✓ Выполнил"


def test_status_badge_right_aligned():
    ind = ThinkingIndicator(state="cancelled", clock=lambda: 0.0)
    ind.set_status("[stopped]", "#888888")
    body = ind._compose_status(Text("■ Отменено"), 30)
    pad = 30 - cell_len("■ Отменено") - cell_len("[stopped]")
    assert body.plain == "■ Отменено" + " " * pad + "[stopped]"


def test_status_badge_dropped_when_too_narrow():
    ind = ThinkingIndicator(state="cancelled", clock=lambda: 0.0)
    ind.set_status("[stopped]", "#888888")
    # ширины не хватает на pad>=1 → бейдж опускается
    body = ind._compose_status(Text("■ Отменено"), 11)
    assert body.plain == "■ Отменено"
