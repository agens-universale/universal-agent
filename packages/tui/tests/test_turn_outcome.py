"""Словарь итогов хода: одни глифы и слова в ленте и в `/sessions`."""

from __future__ import annotations

from types import SimpleNamespace

import pytest
from gc_tui.palette import DIM, ERR, OK, RUN_COLOR
from gc_tui.services.turn_outcome import (
    OUTCOMES,
    outcome_of,
    settled_status,
)


@pytest.mark.parametrize(
    ("status", "glyph", "label"),
    [
        ("completed", "✓", "выполнено"),
        ("failed", "✕", "ошибка"),
        ("cancelled", "■", "остановлено"),
        ("stopped", "■", "остановлено"),
        ("running", "●", "идёт"),
        ("asking", "?", "ждёт ответа"),
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
        ("asking", "? ждёт ответа", RUN_COLOR),
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


#: Все слова статуса, которые доходят до заголовка вкладки: состояние
#: раннера и вопроса, `meta.tui.status` и устаревший `meta.status`.
_TITLE_STATUSES = (
    "running",
    "asking",
    "completed",
    "stopped",
    "failed",
    "unknown_failed",
)


@pytest.mark.parametrize("status", _TITLE_STATUSES)
def test_title_shares_glyph_with_sessions(status):
    from gc_tui.services.terminal_title import format_terminal_title

    glyph = outcome_of(status).glyph
    assert format_terminal_title("x", status) == f"✦ GigaCode {glyph} x"
    assert f"{glyph} {outcome_of(status).list_label}" in (
        _meta_line(status).plain
    )


def test_one_glyph_table():
    glyphs = {s: outcome_of(s).glyph for s in _TITLE_STATUSES}
    assert glyphs == {
        "running": "●",
        "asking": "?",
        "completed": "✓",
        "stopped": "■",
        "failed": "✕",
        "unknown_failed": "✕",
    }


@pytest.mark.parametrize(
    "word,live,shown",
    [
        ("running", False, "interrupted"),
        ("running", True, "running"),
        ("completed", False, "completed"),
        (None, False, None),
    ],
)
def test_running_without_live_run_reads_interrupted(word, live, shown):
    assert settled_status(word, live=live) == shown


def test_interrupted_shares_stop_glyph():
    assert outcome_of("interrupted").glyph == outcome_of("stopped").glyph
    assert outcome_of("interrupted").list_label == "прервано"


# --- статус ↔ слово ↔ состояние строки: один словарь (решение P3b 21) ----


@pytest.mark.parametrize(
    ("status", "word"),
    [
        ("completed", "completed"),
        ("stopped", "cancelled"),
        ("failed", "failed"),
        ("unknown_failed", "failed"),
        ("interrupted", "interrupted"),
        ("running", None),
        ("asking", None),
        (None, None),
    ],
)
def test_status_word_of_finished_turn(status, word):
    from gc_tui.services.turn_outcome import word_of_status

    assert word_of_status(status) == word


@pytest.mark.parametrize(
    ("word", "state"),
    [
        ("completed", "done"),
        ("stopped", "cancelled"),
        ("cancelled", "cancelled"),
        ("failed", "failed"),
        ("interrupted", "interrupted"),
        ("stopped_steps", "stopped_steps"),
    ],
)
def test_indicator_state_of_word(word, state):
    from gc_tui.services.turn_outcome import indicator_state_of

    assert indicator_state_of(word) == state


@pytest.mark.parametrize(
    ("word", "status"),
    [
        ("completed", "completed"),
        ("stopped", "stopped"),
        ("cancelled", "stopped"),
        ("failed", "failed"),
        ("stopped_judge", "stopped"),
    ],
)
def test_meta_status_of_word(word, status):
    from gc_tui.services.turn_outcome import meta_status_of

    assert meta_status_of(word) == status
