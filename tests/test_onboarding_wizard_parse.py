import pytest
from gc_cli import onboarding_wizard as wz


def test_parse_single_ok_and_normalizes():
    assert wz.parse_single("2", 3) == 2
    assert wz.parse_single(" 01 ", 3) == 1
    assert wz.parse_single("", 3) is None


def test_parse_single_rejects_multi_and_out_of_range():
    with pytest.raises(wz.InvalidChoice):
        wz.parse_single("1,2", 3)
    with pytest.raises(wz.InvalidChoice):
        wz.parse_single("9", 3)
    with pytest.raises(wz.InvalidChoice):
        wz.parse_single("x", 3)


def test_parse_multi_ok_dedup_sorted_and_empty():
    assert wz.parse_multi("1,3", 3) == [1, 3]
    assert wz.parse_multi("3 1 1", 3) == [1, 3]
    assert wz.parse_multi("", 3) == []


def test_parse_multi_rejects_bad_token():
    with pytest.raises(wz.InvalidChoice):
        wz.parse_multi("1,9", 3)
    with pytest.raises(wz.InvalidChoice):
        wz.parse_multi("1,abc", 3)
