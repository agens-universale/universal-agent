from gc_tui.screens.main import resolve_live_terminal_indicator_state as R


def test_step_limit_kind_preferred():
    assert (
        R(
            last_indicator_kind="stopped_steps",
            status="stopped",
            stop_requested=False,
        )
        == "stopped_steps"
    )


def test_tool_errors_kind_preferred():
    assert (
        R(
            last_indicator_kind="stopped_errors",
            status="stopped",
            stop_requested=False,
        )
        == "stopped_errors"
    )


def test_token_limit_kind_preferred():
    assert (
        R(
            last_indicator_kind="stopped_tokens",
            status="stopped",
            stop_requested=False,
        )
        == "stopped_tokens"
    )


def test_tool_call_limit_kind_preferred():
    assert (
        R(
            last_indicator_kind="stopped_tool_calls",
            status="stopped",
            stop_requested=False,
        )
        == "stopped_tool_calls"
    )


def test_completed_maps_to_done():
    assert (
        R(last_indicator_kind="done", status="completed", stop_requested=False)
        == "done"
    )


def test_direct_stop_guard_still_applies():
    # last_indicator.kind not a limit kind → fall to status path; completed+stop → cancelled
    assert (
        R(last_indicator_kind="done", status="completed", stop_requested=True)
        == "cancelled"
    )


def test_legacy_no_last_indicator_uses_status():
    assert (
        R(last_indicator_kind=None, status="stopped", stop_requested=False)
        == "cancelled"
    )


def test_judge_unavailable_has_its_own_ending():
    """Проверка действий недоступна — не «превышен лимит ошибок»: человеку
    нужно знать, что дело в проверке и что помогает смена режима."""
    from gc_tui.screens.main import (
        resolve_live_terminal_indicator_state,
        resolve_terminal_outcome,
    )
    from gc_tui.widgets.thinking_indicator import (
        _TERMINAL_ENDINGS,
        TERMINAL_INDICATOR_STATES,
    )

    outcome = resolve_terminal_outcome(
        runner_error=None,
        stop_requested=False,
        terminal_outcome="judge_unavailable",
    )
    assert outcome == "stopped_judge"
    assert outcome in TERMINAL_INDICATOR_STATES
    assert "проверка действий недоступна" in _TERMINAL_ENDINGS[outcome][1]
    assert "лимит" not in _TERMINAL_ENDINGS[outcome][1]
    # после перезапуска исход восстанавливается по сохранённому виду индикатора
    assert (
        resolve_live_terminal_indicator_state(
            last_indicator_kind="stopped_judge",
            status="stopped",
            stop_requested=False,
        )
        == "stopped_judge"
    )
