"""format_duration: человекочитаемая длительность Nс / Nм Mс / Nч Mм."""
import pytest

from tui.services.duration_format import format_duration


@pytest.mark.parametrize("seconds,expected", [
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
])
def test_format_duration(seconds, expected):
    assert format_duration(seconds) == expected
