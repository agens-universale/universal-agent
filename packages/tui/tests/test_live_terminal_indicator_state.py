from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_tui.screens.main import resolve_live_terminal_indicator_state as R


def _outcome(status, reason=None):
    return TurnOutcome(
        status=status,
        reason=reason,
        opened_by=None,
        started_at="2026-10-22T11:32:00Z",
        ended_at="2026-10-22T11:32:12Z",
        duration_sec=12.0,
        error=None,
    )


def test_record_limit_outcomes_win_over_status():
    """Запись итога несёт точный лимитный исход, статус меты — нет."""
    for reason, state in (
        ("step_limit", "stopped_steps"),
        ("tool_errors", "stopped_errors"),
        ("token_limit", "stopped_tokens"),
        ("tool_call_limit", "stopped_tool_calls"),
        ("judge_unavailable", "stopped_judge"),
    ):
        got = R(
            outcome=_outcome("stopped", reason),
            status="stopped",
            stop_requested=False,
        )
        assert got == state


def test_record_done_stop_and_error():
    assert (
        R(outcome=_outcome("done"), status="stopped", stop_requested=True)
        == "done"
    )
    assert (
        R(
            outcome=_outcome("stopped", USER_STOP),
            status=None,
            stop_requested=False,
        )
        == "cancelled"
    )
    assert (
        R(outcome=_outcome("error"), status=None, stop_requested=False)
        == "failed"
    )


def test_completed_maps_to_done():
    assert R(outcome=None, status="completed", stop_requested=False) == "done"


def test_direct_stop_guard_still_applies():
    # Записи нет → путь статуса; completed + stop → cancelled.
    assert (
        R(outcome=None, status="completed", stop_requested=True) == "cancelled"
    )


def test_no_record_uses_status():
    assert (
        R(outcome=None, status="stopped", stop_requested=False) == "cancelled"
    )
    assert R(outcome=None, status=None, stop_requested=False) is None


def test_judge_unavailable_has_its_own_ending():
    """Проверка действий недоступна — не «превышен лимит ошибок»: человеку
    нужно знать, что дело в проверке и что помогает смена режима."""
    from gc_tui.services.turn_outcome import OUTCOMES, outcome_word
    from gc_tui.widgets.thinking_indicator import TERMINAL_INDICATOR_STATES

    outcome = outcome_word(
        failed=False, stopped=False, reason="judge_unavailable"
    )
    assert outcome == "stopped_judge"
    assert outcome in TERMINAL_INDICATOR_STATES
    assert "проверка действий недоступна" in OUTCOMES[outcome].sentence
    assert "лимит" not in OUTCOMES[outcome].sentence
    # после перезапуска исход восстанавливается по записи итога хода
    assert (
        R(
            outcome=_outcome("stopped", "judge_unavailable"),
            status="stopped",
            stop_requested=False,
        )
        == "stopped_judge"
    )
