"""Unit tests for is_grep_native_safe / is_safe_grep_include."""

from gc_core.utils.pattern_native_gate import (
    is_grep_native_safe,
    is_safe_grep_include,
)


def test_denylist_python_markers():
    for pat in (
        r"\d+",
        r"(?i)x",
        r"\s",
        r"\bword",
        r"a{2,3}",
        r"(?:x)",
        r"\1",
    ):
        assert is_grep_native_safe(pat, query_mode="python-re") is None


def test_literal_and_simple_ere_ok():
    lit = is_grep_native_safe("TODO", query_mode="python-re")
    assert lit is not None and lit.fixed is True

    ere = is_grep_native_safe(r"foo|bar", query_mode="python-re")
    assert ere is not None and ere.fixed is False

    text = is_grep_native_safe("a.b", query_mode="text")
    assert text is not None and text.fixed is True


def test_dotall_forbidden():
    assert is_grep_native_safe("a.b", query_mode="python-re-dotall") is None


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


def test_text_mode_newline_pattern_forbidden():
    assert is_grep_native_safe("};\n}", query_mode="text") is None


def test_python_re_literal_with_newline_forbidden():
    assert is_grep_native_safe("foo\nbar", query_mode="python-re") is None


def test_carriage_return_forbidden_in_every_mode():
    assert is_grep_native_safe("foo\rbar", query_mode="text") is None
    assert is_grep_native_safe("foo\rbar", query_mode="python-re") is None


def test_nul_byte_forbidden_in_every_mode():
    assert is_grep_native_safe("foo\x00bar", query_mode="text") is None
    assert is_grep_native_safe("foo\x00bar", query_mode="python-re") is None


def test_tab_and_other_control_chars_forbidden_in_text_mode():
    assert is_grep_native_safe("foo\tbar", query_mode="text") is None
    assert is_grep_native_safe("foo\x01bar", query_mode="text") is None
    assert is_grep_native_safe("foo\x7fbar", query_mode="text") is None


def test_python_re_backslash_goes_to_python():
    """``\\.py`` is a literal dot in Python re; grep -F would search for a slash."""
    assert is_grep_native_safe(r"\.py", query_mode="python-re") is None
    assert is_grep_native_safe(r"foo\(bar", query_mode="python-re") is None
    assert is_grep_native_safe(r"a\\b", query_mode="python-re") is None


def test_text_mode_backslash_stays_fixed():
    """query_mode=text: the agent asked for those bytes, including the slash."""
    plan = is_grep_native_safe(r"\.py", query_mode="text")
    assert plan is not None and plan.fixed is True
    assert plan.pattern == r"\.py"


def test_ordinary_patterns_keep_their_plans():
    text = is_grep_native_safe("a.b", query_mode="text")
    assert text is not None and text.fixed is True

    lit = is_grep_native_safe("TODO", query_mode="python-re")
    assert lit is not None and lit.fixed is True

    ere = is_grep_native_safe("foo.*bar", query_mode="python-re")
    assert ere is not None and ere.fixed is False
