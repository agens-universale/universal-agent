from gc_tui.screens.main import resolve_live_terminal_indicator_state as R


def test_completed_maps_to_done():
    assert R(status="completed", stop_requested=False) == "done"


def test_direct_stop_guard_still_applies():
    # completed + stop → cancelled.
    assert R(status="completed", stop_requested=True) == "cancelled"


def test_no_record_uses_status():
    assert R(status="stopped", stop_requested=False) == "cancelled"
    assert R(status=None, stop_requested=False) is None


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
