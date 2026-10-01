"""Таблица stopReason."""

import pytest

from gc_acp.stop_reason import stop_reason


@pytest.mark.parametrize(
    ("outcome", "cancelled", "expected"),
    [
        (None, False, "end_turn"),
        (None, True, "cancelled"),
        ("token_limit", False, "max_tokens"),
        ("token_limit", True, "cancelled"),
        ("step_limit", False, "max_turn_requests"),
        ("tool_call_limit", False, "max_turn_requests"),
        ("tool_errors", False, "end_turn"),
        ("anything_else", False, "end_turn"),
    ],
)
def test_stop_reason_table(outcome, cancelled, expected):
    assert stop_reason(outcome, cancelled=cancelled) == expected
