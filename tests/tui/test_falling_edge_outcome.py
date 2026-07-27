# tests/tui/test_falling_edge_outcome.py
# Проверяем чистую функцию выбора outcome (вынесем её из спадающего фронта).
from tui.screens.main import resolve_terminal_outcome

def test_step_limit_maps_to_stopped_steps():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="step_limit",
    ) == "stopped_steps"

def test_tool_errors_maps_to_stopped_errors():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="tool_errors",
    ) == "stopped_errors"

def test_token_limit_maps_to_stopped_tokens():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="token_limit",
    ) == "stopped_tokens"

def test_tool_call_limit_maps_to_stopped_tool_calls():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome="tool_call_limit",
    ) == "stopped_tool_calls"

def test_error_and_stop_take_priority():
    assert resolve_terminal_outcome(
        runner_error="boom", stop_requested=False, terminal_outcome="step_limit",
    ) == "failed"
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=True, terminal_outcome="step_limit",
    ) == "stopped"

def test_error_and_stop_take_priority_over_new_limits():
    assert resolve_terminal_outcome(
        runner_error="boom", stop_requested=False, terminal_outcome="token_limit",
    ) == "failed"
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=True, terminal_outcome="tool_call_limit",
    ) == "stopped"

def test_default_completed():
    assert resolve_terminal_outcome(
        runner_error=None, stop_requested=False, terminal_outcome=None,
    ) == "completed"
