"""format_duration: человекочитаемая длительность Nс / Nм Mс / Nч Mм."""

import pytest
from gc_tui.services.duration_format import (
    format_compact_int,
    format_duration,
    format_round_int,
    format_size,
)


@pytest.mark.parametrize(
    "seconds,expected",
    [
        (0, "0с"),
        (5, "5с"),
        (59, "59с"),
        (60, "1м 0с"),
        (65, "1м 5с"),
        (599, "9м 59с"),
        (3599, "59м 59с"),
        (3600, "1ч 0м"),
        (3725, "1ч 2м"),
        (-3, "0с"),
        (65.9, "1м 5с"),  # дробное усекается к int
    ],
)
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1.0k"),
        (1200, "1.2k"),
        (12345, "12.3k"),
        (3100, "3.1k"),
    ],
)
def test_format_compact_int(n, expected):
    assert format_compact_int(n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0"),
        (999, "999"),
        (1000, "1k"),
        (6_100, "6k"),
        (128_300, "128k"),
        (999_999, "999k"),
        (1_000_000, "1.0M"),
        (1_234_567, "1.2M"),
    ],
)
def test_format_round_int(n, expected):
    assert format_round_int(n) == expected


@pytest.mark.parametrize(
    "n,expected",
    [
        (0, "0 Б"),
        (512, "512 Б"),
        (12_288, "12 КБ"),
        (3_565_158, "3,4 МБ"),
    ],
)
def test_format_size(n, expected):
    assert format_size(n) == expected
