"""status_badge_color: цвет палитры по тексту бейджа."""
from tui.services.status_badge import status_badge_color
from tui.palette import RUN_COLOR, ACCENT, OK, DIM, ERR


def test_known_statuses():
    assert status_badge_color("[running]") == RUN_COLOR
    assert status_badge_color("[asking]") == ACCENT
    assert status_badge_color("[completed]") == OK
    assert status_badge_color("[stopped]") == DIM
    assert status_badge_color("[failed]") == ERR


def test_unknown_falls_back_dim():
    assert status_badge_color("[whatever]") == DIM
