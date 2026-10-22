"""Словарь итогов хода: одни глифы и слова в ленте и в `/sessions`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gc_tui.palette import DIM, ERR, OK, RUN_COLOR
from gc_tui.services.turn_outcome import OUTCOMES, outcome_of


@pytest.mark.parametrize(
    ("status", "glyph", "label"),
    [
        ("completed", "✓", "выполнено"),
        ("failed", "✕", "ошибка"),
        ("cancelled", "■", "остановлено"),
        ("stopped", "■", "остановлено"),
        ("running", "●", "идёт"),
    ],
)
def test_vocabulary(status, glyph, label):
    outcome = outcome_of(status)
    assert (outcome.glyph, outcome.list_label) == (glyph, label)


def test_unknown_status_has_no_outcome():
    assert outcome_of("weird") is None
    assert outcome_of(None) is None


def test_trace_sentences_are_kept():
    assert OUTCOMES["completed"].sentence == "Выполнил"
    assert OUTCOMES["failed"].sentence == "Ошибка"
    assert OUTCOMES["cancelled"].sentence == "Отменено"


def _meta_line(status):
    from gc_tui.screens.sessions import SessionsScreen

    scr = SessionsScreen.__new__(SessionsScreen)
    scr._task_display_status = lambda task: status
    scr._session_cost_label = lambda task: None
    scr._session_is_busy = lambda tid: False
    task = SimpleNamespace(
        updated_at="2026-05-30T10:00:00+00:00",
        tui_meta={},
        messages_count=1,
        thread_id="t" * 32,
    )
    return scr._meta_line(task)


@pytest.mark.parametrize(
    ("status", "text", "color"),
    [
        ("completed", "✓ выполнено", OK),
        ("failed", "✕ ошибка", ERR),
        ("stopped", "■ остановлено", DIM),
        ("running", "● идёт", RUN_COLOR),
    ],
)
def test_sessions_list_shows_labels(status, text, color):
    line = _meta_line(status)
    assert text in line.plain
    assert "[" not in line.plain
    styles = [
        str(s.style) for s in line.spans if line.plain[s.start : s.end] == text
    ]
    assert styles == [color]


def test_sessions_list_unknown_status_as_is_dim():
    line = _meta_line("weird")
    assert "weird" in line.plain
    assert "[weird]" not in line.plain
    styles = [
        str(s.style)
        for s in line.spans
        if line.plain[s.start : s.end] == "weird"
    ]
    assert styles == [DIM]


@pytest.mark.parametrize(
    ("state", "status"),
    [("done", "completed"), ("failed", "failed"), ("cancelled", "stopped")],
)
def test_indicator_and_sessions_share_glyph(state, status):
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    head = ThinkingIndicator(state=state, frozen_elapsed=1)
    glyph = head._render_text().plain.split()[0]
    assert glyph == outcome_of(status).glyph
    assert glyph in _meta_line(status).plain
