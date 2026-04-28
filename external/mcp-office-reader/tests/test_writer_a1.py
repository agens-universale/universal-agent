import pytest

from mcp_office_reader.writer_a1 import a1_cell, parse_a1_range


def test_single_cell():
    assert parse_a1_range("B5") == (2, 5, 2, 5)


def test_range():
    assert parse_a1_range("A1:C3") == (1, 1, 3, 3)


def test_multi_letter_column():
    assert parse_a1_range("AA1:AB2") == (27, 1, 28, 2)


def test_a1_cell_roundtrip():
    assert a1_cell(1, 1) == "A1"
    assert a1_cell(27, 10) == "AA10"


@pytest.mark.parametrize("bad", ["", "1A", "A", "A0", "A1:", ":B2", "!", "A1:B"])
def test_invalid_inputs_raise(bad):
    with pytest.raises(ValueError):
        parse_a1_range(bad)


def test_reversed_range_normalized():
    # C3:A1 should normalize to A1:C3
    assert parse_a1_range("C3:A1") == (1, 1, 3, 3)
