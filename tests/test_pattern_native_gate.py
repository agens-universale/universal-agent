"""Unit tests for is_grep_native_safe / is_safe_grep_include."""
from utils.pattern_native_gate import is_grep_native_safe, is_safe_grep_include


def test_denylist_python_markers():
    for pat in (r"\d+", r"(?i)x", r"\s", r"\bword", r"a{2,3}", r"(?:x)", r"\1"):
        assert is_grep_native_safe(pat, query_mode="python-re") is None


def test_literal_and_simple_ere_ok():
    lit = is_grep_native_safe("TODO", query_mode="python-re")
    assert lit is not None and lit.fixed is True

    ere = is_grep_native_safe(r"foo|bar", query_mode="python-re")
    assert ere is not None and ere.fixed is False

    text = is_grep_native_safe("a.b", query_mode="text")
    assert text is not None and text.fixed is True


def test_dotall_forbidden():
    assert (
        is_grep_native_safe("a.b", query_mode="python-re-dotall") is None
    )


def test_empty_forbidden():
    assert is_grep_native_safe("", query_mode="text") is None


def test_safe_include():
    assert is_safe_grep_include("*.py")
    assert is_safe_grep_include("*_test.py")
    assert is_safe_grep_include("Makefile")
    assert not is_safe_grep_include("src/*.py")
    assert not is_safe_grep_include("**/*.py")
    assert not is_safe_grep_include("*[ab].py")
    assert not is_safe_grep_include("")
