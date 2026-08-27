"""Denylist gate: which user patterns may run on native grep.

User-facing canon remains Python ``re``. Native grep is an adapter only after
this gate passes. Fail closed → Python path.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

# Inline / extended groups: (?i), (?:), (?P<…>), …
_INLINE_GROUP = re.compile(r"\(\?")

# Python-ish escapes that are wrong or different under ERE / often silent-wrong.
_BAD_ESCAPES = re.compile(
    r"\\[dDwWsSbBAZz]|\\[1-9]"
)

# Quantifier braces — Python by default (ERE supports them but portability/semantics).
_BRACES = re.compile(r"\{[0-9]+(?:,[0-9]*)?\}")

@dataclass(frozen=True)
class NativeGrepPlan:
    """How to invoke grep when gate allows native."""

    fixed: bool  # True → grep -F
    pattern: str  # pattern string for argv (no (?i))
    case_insensitive: bool


def is_safe_grep_include(file_pattern: Optional[str]) -> bool:
    """Basename-only globs safe for ``grep --include`` (no path/charclass/**)."""
    if file_pattern is None:
        return False
    if not isinstance(file_pattern, str):
        return False
    s = file_pattern.strip()
    if not s:
        return False
    if "/" in s or "\\" in s:
        return False
    if "**" in s:
        return False
    if "[" in s or "]" in s:
        return False
    # alphabet: literals + * + ? only (no escapes in v1)
    for ch in s:
        if ch in "*?":
            continue
        # allow typical filename literals
        if ch.isalnum() or ch in "._-+~@=%":
            continue
        return False
    return True


def _has_control_chars(pattern: str) -> bool:
    """True if pattern carries a char grep argv must never receive.

    A newline is a pattern separator for grep, so it would silently widen the
    match into an OR; the rest are equally unsafe to hand to argv.
    """
    for ch in pattern:
        if ch in "\x00\n\r\t":
            return True
        o = ord(ch)
        if o < 32 or o == 127:
            return True
    return False


def is_grep_native_safe(
    pattern: str,
    *,
    query_mode: str,
    case_insensitive: bool = False,
) -> Optional[NativeGrepPlan]:
    """Return a native plan or None → Python.

    ``query_mode``: ``text`` | ``python-re`` | ``python-re-dotall``.
    Dotall / multiline-span modes always Python.
    """
    if not isinstance(pattern, str) or pattern == "":
        return None
    if query_mode == "python-re-dotall":
        return None
    if query_mode not in ("text", "python-re"):
        return None

    # control chars are unsafe for grep argv in every mode → fail closed
    if _has_control_chars(pattern):
        return None

    # text mode → fixed string
    if query_mode == "text":
        return NativeGrepPlan(
            fixed=True,
            pattern=pattern,
            case_insensitive=bool(case_insensitive),
        )

    # python-re: denylist
    if _INLINE_GROUP.search(pattern):
        return None
    if _BAD_ESCAPES.search(pattern):
        return None
    if _BRACES.search(pattern):
        return None

    # COMPLEX-looking: if pattern has no regex meta → fixed; else simple ERE
    # Meta for "is this literal?"
    if _is_python_re_literal(pattern):
        return NativeGrepPlan(
            fixed=True,
            pattern=pattern,
            case_insensitive=bool(case_insensitive),
        )

    # Simple ERE-shaped: allow . * + ? | ( ) ^ $ [ ] and escapes of meta
    if not _looks_like_simple_ere(pattern):
        return None

    return NativeGrepPlan(
        fixed=False,
        pattern=pattern,
        case_insensitive=bool(case_insensitive),
    )


def _is_python_re_literal(pattern: str) -> bool:
    """True if pattern has no unescaped Python re metacharacters."""
    i = 0
    meta = set(".*+?|()[]{}^$")
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            if i + 1 >= len(pattern):
                return False
            i += 2
            continue
        if ch in meta:
            return False
        i += 1
    return True


def _looks_like_simple_ere(pattern: str) -> bool:
    """Conservative allow for grep -E after denylist.

    Rejects nested complexity we do not want to reason about: lookbehind-like
    already blocked by (? ; backrefs blocked; braces blocked.
    """
    return not _has_control_chars(pattern)
