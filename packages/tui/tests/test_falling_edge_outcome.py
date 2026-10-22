# tests/tui/test_falling_edge_outcome.py
# Проверяем чистую функцию выбора outcome (вынесем её из спадающего фронта).
from gc_tui.services.turn_outcome import outcome_word


def test_step_limit_maps_to_stopped_steps():
    assert (
        outcome_word(
            failed=False,
            stopped=False,
            reason="step_limit",
        )
        == "stopped_steps"
    )


def test_tool_errors_maps_to_stopped_errors():
    assert (
        outcome_word(
            failed=False,
            stopped=False,
            reason="tool_errors",
        )
        == "stopped_errors"
    )


def test_token_limit_maps_to_stopped_tokens():
    assert (
        outcome_word(
            failed=False,
            stopped=False,
            reason="token_limit",
        )
        == "stopped_tokens"
    )


def test_tool_call_limit_maps_to_stopped_tool_calls():
    assert (
        outcome_word(
            failed=False,
            stopped=False,
            reason="tool_call_limit",
        )
        == "stopped_tool_calls"
    )


def test_error_and_stop_take_priority():
    assert (
        outcome_word(
            failed=True,
            stopped=False,
            reason="step_limit",
        )
        == "failed"
    )
    assert (
        outcome_word(
            failed=False,
            stopped=True,
            reason="step_limit",
        )
        == "stopped"
    )


def test_error_and_stop_take_priority_over_new_limits():
    assert (
        outcome_word(
            failed=True,
            stopped=False,
            reason="token_limit",
        )
        == "failed"
    )
    assert (
        outcome_word(
            failed=False,
            stopped=True,
            reason="tool_call_limit",
        )
        == "stopped"
    )


def test_default_completed():
    assert (
        outcome_word(
            failed=False,
            stopped=False,
            reason=None,
        )
        == "completed"
    )
