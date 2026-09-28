"""Characterization-тесты краевых случаев glob_to_regex (фиксируют текущее поведение)."""

import pytest
from gc_core.utils.glob_to_regex import glob_to_regex


@pytest.mark.parametrize(
    ("pattern", "expected"),
    [
        (
            "a[bc",
            "a\\[bc",
        ),  # незакрытый класс: '[' литерал, остаток без экранирования
        (
            "a[b*?c",
            "a\\[b*?c",
        ),  # после незакрытого '[' спецсимволы не раскрываются
        ("[!", "\\[!"),
        ("[!ab]", "[^ab]"),
        ("[ab]", "[ab]"),
        ("**", ".*"),
        ("**x", ".*x"),
        ("x/**", "x(?:/.*)?"),
        ("src/**", "src(?:/.*)?"),
        ("a/**/b", "a/(?:.+/)?b"),
        ("a/**b", "a/(?:/.+)?b"),
        ("*.py", "[^/]*\\.py"),
        ("f?.txt", "f.\\.txt"),
        ("", ""),
    ],
)
def test_glob_to_regex_edge_cases(pattern, expected):
    assert glob_to_regex(pattern) == expected


@pytest.mark.parametrize(
    ("pattern", "directory", "sibling"),
    [
        ("src/**", "src", "srcfoo"),
        ("my-dir/**", "my-dir", "my-dix"),
        ("pkg.v1/**", "pkg.v1", "pkg.v2"),
        ("my dir/**", "my dir", "my dirs"),
        ("src*/**", "src-lib", "lib"),
        ("[ab]/**", "a", "ab"),
        ("**/src/**", "nested/src", "nested/srcfoo"),
    ],
)
def test_recursive_suffix_respects_directory_boundary(
    pattern, directory, sibling
):
    from gc_core.utils.glob_to_regex import compile_glob

    regex = compile_glob(pattern)
    for path in (
        directory,
        directory + "/",
        directory + "/x.py",
        directory + "/deep/x.py",
    ):
        assert regex.fullmatch(path), (pattern, path)
    for path in (sibling, sibling + "/x.py"):
        assert regex.fullmatch(path) is None, (pattern, path)
