"""Characterization-тесты _positive_int из utils/llm_chat_client."""

import pytest
from gc_core.utils.llm_chat_client import _positive_int


@pytest.mark.parametrize(
    "raw, expected",
    [
        (None, 7),
        (True, 7),
        (False, 7),
        (5, 5),
        (0, 7),
        (-3, 7),
        (2.9, 2),
        (0.4, 7),
        ("", 7),
        ("   ", 7),
        ("12", 12),
        (" 1_000 ", 1000),
        ("1 500", 1500),
        ("2.7", 2),
        ("0", 7),
        ("-1", 7),
        ("abc", 7),
        ("4k", 4000),
        ("1.5K", 1500),
        ("0k", 7),
        ("0.0001k", 7),
        ("xk", 7),
        ("k", 7),
        ([1], 7),
        ({"a": 1}, 7),
    ],
)
def test_positive_int(raw, expected):
    assert _positive_int(raw, default=7) == expected
