"""Голова строки итога хода: глиф, слово словаря, длительность, время."""

from __future__ import annotations

from datetime import datetime

import pytest
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_tui.services.turn_outcome import kind_of, outcome_word, turn_head

NOW = datetime(2026, 10, 22, 20, 0).astimezone()
ENDED = datetime(2026, 10, 22, 14, 32).astimezone().isoformat()


def _head(word, *, plan=False, dur=None, ended=ENDED):
    return turn_head(
        word, plan=plan, duration_sec=dur, ended_at=ended, now=NOW
    ).plain


def test_completed_has_duration_and_time():
    assert _head("completed", dur=72.0) == "✓ Выполнил за 1м 12с · 14:32"


def test_plan_has_duration_and_time():
    got = _head("completed", plan=True, dur=40.0)
    assert got == "✓ План готов за 40с · 14:32"


def test_cancelled_has_no_duration_even_if_given():
    assert _head("cancelled", dur=41.0) == "■ Отменено · 14:32"


def test_failed_has_no_duration():
    assert _head("failed", dur=5.0) == "✕ Ошибка · 14:32"


def test_limit_has_no_duration():
    got = _head("stopped_steps", dur=5.0)
    assert got == "⏹ Остановлено: превышен лимит шагов · 14:32"


def test_without_time_no_separator():
    assert _head("failed", ended=None) == "✕ Ошибка"
    assert _head("completed", dur=3.0, ended="") == "✓ Выполнил за 3с"


def test_completed_without_duration():
    assert _head("completed") == "✓ Выполнил · 14:32"


def _rec(status, reason=None):
    return TurnOutcome(
        status=status,
        reason=reason,
        opened_by=None,
        started_at=ENDED,
        ended_at=ENDED,
        duration_sec=1.0,
        error=None,
    )


@pytest.mark.parametrize(
    ("status", "reason", "expected"),
    [
        ("done", None, ("completed", False)),
        ("done", "plan_proposed", ("completed", True)),
        ("stopped", USER_STOP, ("cancelled", False)),
        ("stopped", "step_limit", ("stopped_steps", False)),
        ("stopped", "tool_call_limit", ("stopped_tool_calls", False)),
        ("stopped", "token_limit", ("stopped_tokens", False)),
        ("stopped", "tool_errors", ("stopped_errors", False)),
        ("stopped", "judge_unavailable", ("stopped_judge", False)),
        # Причина, которой словарь не знает, — остановка, не «Выполнил».
        ("stopped", None, ("cancelled", False)),
        ("stopped", "new_reason", ("cancelled", False)),
        ("error", None, ("failed", False)),
    ],
)
def test_kind_of_matrix(status, reason, expected):
    assert kind_of(_rec(status, reason)) == expected


def test_outcome_word_priority_and_default():
    def word(failed=False, stopped=False, reason=None):
        return outcome_word(failed=failed, stopped=stopped, reason=reason)

    assert word(failed=True, reason="step_limit") == "failed"
    assert word(stopped=True, reason="step_limit") == "stopped"
    assert word(reason="step_limit") == "stopped_steps"
    assert word(reason="plan_proposed") == "completed"
    assert word() == "completed"
