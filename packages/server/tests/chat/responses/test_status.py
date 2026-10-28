"""Таблица статусов хода."""

import pytest
from gc_server.chat.responses.status import incomplete_details, turn_status


@pytest.mark.parametrize(
    ("outcome", "expected"),
    [
        (None, "completed"),
        ("tool_errors", "completed"),
        ("other", "completed"),
        ("", "completed"),
        ("token_limit", "incomplete"),
        ("step_limit", "incomplete"),
        ("tool_call_limit", "incomplete"),
    ],
)
def test_outcome_table(outcome, expected):
    assert (
        turn_status(outcome=outcome, cancelled=False, failed=False) == expected
    )


def test_cancel_wins_over_limit_and_failure():
    assert (
        turn_status(outcome="token_limit", cancelled=True, failed=True)
        == "cancelled"
    )
    assert turn_status(outcome=None, cancelled=False, failed=True) == "failed"


def test_incomplete_details_only_for_incomplete():
    assert incomplete_details("incomplete", "step_limit") == {
        "reason": "step_limit"
    }
    assert incomplete_details("completed", None) is None
    assert incomplete_details("cancelled", "token_limit") is None
    assert incomplete_details("failed", "token_limit") is None
