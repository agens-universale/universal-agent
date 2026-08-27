"""Strict whitelist scan modes: like / or_simple / affix_or / complex only."""

from __future__ import annotations

import json
import re
from collections import Counter
from pathlib import Path

import pytest

from utils.find_in_files_scan_mode import (
    PatternMode,
    classify_scan_pattern,
    is_literal_string,
)


# ---------------------------------------------------------------------------
# is_literal_string
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "s,ok",
    [
        ("TODO", True),
        ("def ", True),
        ("foo_bar", True),
        ("Привет", True),
        ("a.b", False),
        ("a|b", False),
        ("(x)", False),
        (r"\bfoo", False),
        ("^foo", False),
        ("foo$", False),
        ("", False),
    ],
)
def test_is_literal_string(s, ok):
    assert is_literal_string(s) is ok


# ---------------------------------------------------------------------------
# like
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,lit",
    [
        ("TODO", "TODO"),
        ("dump_storage", "dump_storage"),
        ("Привет", "Привет"),
        ("err42", "err42"),
        ("(?i)hello", "hello"),
        (r"\bfoo\b", None),  # backslash → complex
        ("^foo$", None),  # anchors → complex
        ("foo.bar", None),
        (r"foo\+bar", None),
        (r"foo\.bar", None),
    ],
)
def test_like(pattern, lit):
    sp = classify_scan_pattern(pattern)
    if lit is None:
        assert sp.mode is PatternMode.COMPLEX
        return
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == lit
    assert sp.or_group is None


# ---------------------------------------------------------------------------
# or_simple
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,branches",
    [
        ("foo|bar", ("foo", "bar")),
        ("foo|bar|baz", ("foo", "bar", "baz")),
        ("ALPHA|beta", ("ALPHA", "beta")),
        ("(foo|bar)", ("foo", "bar")),
        (
            "(openai_strict_tools_schema|apply_openai_strict|arguments_validation)",
            (
                "openai_strict_tools_schema",
                "apply_openai_strict",
                "arguments_validation",
            ),
        ),
        (
            "build_tool_specification|required_tools_banned_in_definition|_FileSubagentFactory",
            (
                "build_tool_specification",
                "required_tools_banned_in_definition",
                "_FileSubagentFactory",
            ),
        ),
    ],
)
def test_or_simple_pure(pattern, branches):
    sp = classify_scan_pattern(pattern)
    assert sp.mode is PatternMode.OR_SIMPLE, (sp.mode, sp.or_group)
    assert sp.required_literal is None
    assert set(sp.or_group) == set(branches)
    assert sp.has_prefilter is True


def test_or_with_meta_branch_is_complex():
    sp = classify_scan_pattern(r"\beval\(|\bexec\(")
    assert sp.mode is PatternMode.COMPLEX
    assert sp.has_prefilter is False


def test_or_with_anchor_branch_is_complex():
    """Anchors are meta — no peel; whole OR goes to complex."""
    sp = classify_scan_pattern("^gc:|force_collect")
    assert sp.mode is PatternMode.COMPLEX


# ---------------------------------------------------------------------------
# affix_or
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern,req,ors",
    [
        (
            "def (read_head_bytes|append|write_bytes)",
            "def ",
            ("read_head_bytes", "append", "write_bytes"),
        ),
        (
            "(foo|bar)_suffix",
            "_suffix",
            ("foo", "bar"),
        ),
        (
            "pre_(a|b)_suf",
            "pre_",
            ("a", "b"),
        ),
        (
            "p_(a|b)_longsuf",
            "_longsuf",
            ("a", "b"),
        ),
    ],
)
def test_affix_or(pattern, req, ors):
    sp = classify_scan_pattern(pattern)
    assert sp.mode is PatternMode.AFFIX_OR, (sp.mode, sp.required_literal, sp.or_group)
    assert sp.required_literal == req
    assert set(sp.or_group) == set(ors)


def test_common_prefix_or_is_or_simple_not_pref():
    sp = classify_scan_pattern(
        "request_auxiliary_completion_text|"
        "request_chat_completion_text|"
        "request_summary_text"
    )
    assert sp.mode is PatternMode.OR_SIMPLE
    assert sp.required_literal is None
    assert len(sp.or_group) == 3


def test_quoted_subagent_is_or_simple():
    sp = classify_scan_pattern(
        '"subagent_planner"|"subagent_worker"|"subagent_critic"'
    )
    assert sp.mode is PatternMode.OR_SIMPLE
    assert set(sp.or_group) == {
        '"subagent_planner"',
        '"subagent_worker"',
        '"subagent_critic"',
    }


# ---------------------------------------------------------------------------
# complex
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "pattern",
    [
        ".*",
        ".+",
        "^$",
        ".",
        r"\s+",
        r"\d+",
        r"\w+",
        "",
        r"[abc]+",
        "foo.*bar",
        "English.*words",
        "foo.*bar.*baz",
        r"Line\s+alpha",
        "assert .+ is not None",
        "a.*b.*c",
        r"(?:foo|bar)",
        r"(?i:foo|bar)",
        "a(b|c)d(e|f)",
        r"## L5[\s\S]{0,1500}",
        "foo|bar.*baz",
        r"loop|repeat|залуп|одинак|повтор|same.*call|duplicat",
        r"a+|b+",
        r"foo\s+bar|baz",
        r"(foo.*bar|baz)",
        r"(a+|b+)",
        # top-level | outside the single group: the affix is not required
        "foo(bar|baz)|qux",
        "qux|foo(bar|baz)_suf",
        "a|b(c|d)",
    ],
)
def test_complex(pattern):
    sp = classify_scan_pattern(pattern)
    assert sp.mode is PatternMode.COMPLEX, (
        pattern,
        sp.mode,
        sp.required_literal,
        sp.or_group,
    )
    assert sp.has_prefilter is False


def test_no_and_mode_exists():
    assert not hasattr(PatternMode, "AND")
    assert {m.value for m in PatternMode} == {
        "like",
        "or_simple",
        "affix_or",
        "complex",
    }


def test_invalid_raises():
    with pytest.raises(re.error):
        classify_scan_pattern("(")


# ---------------------------------------------------------------------------
# CI
# ---------------------------------------------------------------------------


def test_case_insensitive_arg():
    sp = classify_scan_pattern("Foo", case_insensitive=True)
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == "Foo"
    assert sp.effective_ci is True


def test_inline_i():
    sp = classify_scan_pattern("(?i)Hello")
    assert sp.effective_ci is True
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == "Hello"


# ---------------------------------------------------------------------------
# Needles shape (what the tool will consume later)
# ---------------------------------------------------------------------------


def test_affix_or_needles_shape():
    sp = classify_scan_pattern("def (read|write)")
    assert sp.mode is PatternMode.AFFIX_OR
    assert sp.required_literal == "def "
    assert set(sp.or_group) == {"read", "write"}


def test_affix_or_both_affixes_picks_longer():
    sp = classify_scan_pattern("p_(a|b)_longsuf")
    assert sp.mode is PatternMode.AFFIX_OR
    assert sp.required_literal == "_longsuf"
    assert set(sp.or_group) == {"a", "b"}


def test_affix_or_with_non_pure_inner_keeps_required_only():
    sp = classify_scan_pattern(r"def (read|write.*x)")
    assert sp.mode is PatternMode.AFFIX_OR
    assert sp.required_literal == "def "
    assert sp.or_group is None


def test_affix_or_dirty_suffix_still_uses_pure_prefix():
    """Suffix may be junk; pure prefix alone is enough for affix_or."""
    sp = classify_scan_pattern("def (a|b).*x")
    assert sp.mode is PatternMode.AFFIX_OR
    assert sp.required_literal == "def "
    assert sp.or_group == ("a", "b")


def test_top_level_or_in_suffix_is_complex_not_affix_or():
    """``foo(bar|baz)|qux`` means ``(foo(bar|baz))|(qux)`` — ``foo`` is not required."""
    sp = classify_scan_pattern("foo(bar|baz)|qux")
    assert sp.mode is PatternMode.COMPLEX
    assert sp.required_literal is None
    assert sp.or_group is None
    assert sp.has_prefilter is False


def test_top_level_or_in_prefix_is_complex_not_affix_or():
    """Same on the other side: ``_suf`` is not required by ``qux|foo(bar|baz)_suf``."""
    sp = classify_scan_pattern("qux|foo(bar|baz)_suf")
    assert sp.mode is PatternMode.COMPLEX
    assert sp.required_literal is None
    assert sp.or_group is None
    assert sp.has_prefilter is False


@pytest.mark.parametrize(
    "pattern,mode,req",
    [
        ("foo(bar|baz)", PatternMode.AFFIX_OR, "foo"),
        ("def (a|b).*x", PatternMode.AFFIX_OR, "def "),
        ("a|b|c", PatternMode.OR_SIMPLE, None),
        ("TODO", PatternMode.LIKE, "TODO"),
    ],
)
def test_no_top_level_or_keeps_previous_classification(pattern, mode, req):
    """Patterns without a top-level ``|`` outside the group are untouched."""
    sp = classify_scan_pattern(pattern)
    assert sp.mode is mode
    assert sp.required_literal == req


def test_complex_has_no_needles():
    sp = classify_scan_pattern("foo.*bar")
    assert sp.mode is PatternMode.COMPLEX
    assert sp.has_prefilter is False


# ---------------------------------------------------------------------------
# Whitespace is significant (no strip of body)
# ---------------------------------------------------------------------------


def test_leading_space_kept_in_like_needle():
    """Needles must match the same string re.compile sees — no .strip()."""
    sp = classify_scan_pattern(" foo")
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == " foo"


def test_trailing_space_on_or_branch_is_not_or_simple():
    """Trailing space on a branch is part of the branch; still pure OR."""
    sp = classify_scan_pattern("a|b ")
    assert sp.mode is PatternMode.OR_SIMPLE
    assert sp.or_group == ("a", "b ")


def test_whitespace_only_body_is_like_not_stripped_to_complex():
    """Spaces-only pattern is a pure literal, not empty after strip."""
    sp = classify_scan_pattern("  ")
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == "  "


def test_ci_prefix_then_spaces_only_is_like():
    sp = classify_scan_pattern("(?i)  ")
    assert sp.mode is PatternMode.LIKE
    assert sp.required_literal == "  "
    assert sp.effective_ci is True


# ---------------------------------------------------------------------------
# Corpus smoke
# ---------------------------------------------------------------------------


def test_corpus_scan_modes():
    path = Path("docs_md/actual/search_regexes_real.jsonl")
    if not path.is_file():
        pytest.skip("corpus missing")

    counts: Counter[str] = Counter()
    fails = 0
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        pattern = row.get("regex") or row.get("pattern")
        if not pattern:
            continue
        try:
            sp = classify_scan_pattern(pattern)
        except re.error:
            fails += 1
            continue
        counts[sp.mode.value] += 1
        if sp.mode is PatternMode.LIKE:
            assert sp.required_literal and sp.or_group is None
        elif sp.mode is PatternMode.OR_SIMPLE:
            assert sp.required_literal is None
            assert sp.or_group and len(sp.or_group) >= 2
        elif sp.mode is PatternMode.AFFIX_OR:
            assert sp.required_literal
        else:
            assert not sp.has_prefilter

    total = sum(counts.values())
    assert total >= 800
    print(f"\n=== scan mode corpus === total={total} fails={fails} counts={dict(counts)}")
    assert counts["like"] >= 100
    assert counts["or_simple"] >= 100
    assert counts["complex"] >= 200
    assert "and" not in counts
