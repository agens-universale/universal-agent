"""Characterization-тесты краевых случаев glob_to_regex (фиксируют текущее поведение)."""

import pytest

from gc_core.utils.glob_to_regex import glob_to_regex


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        ("a[bc", "a\\[bc"),  # незакрытый класс: '[' литерал, остаток без экранирования
        ("a[b*?c", "a\\[b*?c"),  # после незакрытого '[' спецсимволы не раскрываются
        ("[!", "\\[!"),
        ("[!ab]", "[^ab]"),
        ("[ab]", "[ab]"),
        ("**", ".*"),
        ("**x", ".*x"),
        ("x/**", "x.*"),
        ("src/**", "src.*"),
        ("a/**/b", "a/(?:.+/)?b"),
        ("a/**b", "a/(?:/.+)?b"),
        ("*.py", "[^/]*\\.py"),
        ("f?.txt", "f.\\.txt"),
        ("", ""),
    ],
)
def test_glob_to_regex_edge_cases(pattern, expected):
    assert glob_to_regex(pattern) == expected
