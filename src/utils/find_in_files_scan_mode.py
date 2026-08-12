"""Scan-mode classification for ``fs_find_in_files``.

Simple shape detector (no AST, no generic regex parser):

1. strip leading ``(?i)`` (shape only; CI still from ``re.compile``)
2. whole body is a pure literal → ``like``
3. ``a|b|…`` or ``(a|b|…)`` with every branch a pure literal → ``or_simple``
4. exactly one ``(…)`` group; prefix and/or suffix is a pure literal → ``affix_or``
5. else → ``complex``

A pure literal is a non-empty string with no Python ``re`` metacharacters
and no backslash.

Public API:
    - :class:`PatternMode`
    - :class:`Needles`
    - :class:`ScanPattern`
    - :func:`classify_scan_pattern`
    - :func:`is_literal_string`

Prefilter execution (``in`` checks on file text) lives in the search tool,
not here — this module only classifies the pattern and extracts needles.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from enum import Enum

__all__ = [
    "PatternMode",
    "Needles",
    "ScanPattern",
    "classify_scan_pattern",
    "is_literal_string",
]

#: Python ``re`` metacharacters (and ``\\``). Any of these → not a pure literal.
_META = frozenset(".*+?|()[]{}^$\\")


class PatternMode(str, Enum):
    """Scan-budget / prefilter policy label."""

    LIKE = "like"
    OR_SIMPLE = "or_simple"
    AFFIX_OR = "affix_or"
    COMPLEX = "complex"


@dataclass(frozen=True)
class Needles:
    """Sound substring prefilter payload.

    * ``like`` → ``required_literal`` set
    * ``or_simple`` → ``or_group`` set (all pure)
    * ``affix_or`` → ``required_literal`` set; ``or_group`` if inner OR is pure
    * ``complex`` → both empty
    """

    required_literal: str | None
    or_group: tuple[str, ...] | None
    effective_ci: bool

    @property
    def has_prefilter(self) -> bool:
        return self.required_literal is not None or self.or_group is not None


@dataclass(frozen=True)
class ScanPattern:
    """Classified pattern: mode + needles."""

    mode: PatternMode
    needles: Needles

    @property
    def required_literal(self) -> str | None:
        return self.needles.required_literal

    @property
    def or_group(self) -> tuple[str, ...] | None:
        return self.needles.or_group

    @property
    def effective_ci(self) -> bool:
        return self.needles.effective_ci

    @property
    def has_prefilter(self) -> bool:
        return self.needles.has_prefilter


def _strip_ci_prefix(pattern: str) -> str:
    """Remove a single leading ``(?i)`` if present."""
    if pattern.startswith("(?i)"):
        return pattern[4:]
    return pattern


def is_literal_string(s: str) -> bool:
    """True if ``s`` is non-empty and has no regex metacharacters / backslash."""
    if not s:
        return False
    return not any(ch in _META for ch in s)


def _pure_or_branches(body: str) -> tuple[str, ...] | None:
    """Split ``body`` on ``|``; return branches if every one is a pure literal.

    Optional outer ``(…)`` is stripped first (``(a|b)`` same as ``a|b``).
    """
    if body.startswith("(") and body.endswith(")"):
        body = body[1:-1]
    if "|" not in body:
        return None
    parts = body.split("|")
    if len(parts) < 2 or not all(is_literal_string(p) for p in parts):
        return None
    # dedupe, keep order
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        if p not in seen:
            seen.add(p)
            out.append(p)
    if len(out) < 2:
        return None
    return tuple(out)


def _split_single_group(body: str) -> tuple[str, str, str] | None:
    """Split into prefix / inner / suffix around exactly one ``(…)``."""
    if body.count("(") != 1 or body.count(")") != 1:
        return None
    open_i = body.find("(")
    close_i = body.find(")")
    if open_i < 0 or close_i <= open_i:
        return None
    pre = body[:open_i]
    inner = body[open_i + 1 : close_i]
    suf = body[close_i + 1 :]
    # reject special groups: (?:…), (?i:…), lookarounds, …
    if inner.startswith("?"):
        return None
    return pre, inner, suf


def _pick_longer(a: str | None, b: str | None) -> str | None:
    """Longer of two optional affixes (prefix wins on tie)."""
    if a is None:
        return b
    if b is None:
        return a
    return a if len(a) >= len(b) else b


def classify_scan_pattern(
    pattern: str,
    *,
    case_insensitive: bool = False,
) -> ScanPattern:
    """Classify ``pattern`` with the whole-pattern whitelist.

    Raises:
        re.error: If ``pattern`` is not a valid Python regular expression.
    """
    flags = re.DOTALL | re.MULTILINE
    if case_insensitive:
        flags |= re.IGNORECASE
    compiled = re.compile(pattern, flags)
    effective_ci = bool(compiled.flags & re.IGNORECASE)

    # No .strip(): needles/mode must match the same string re.compile sees.
    body = _strip_ci_prefix(pattern)
    if not body:
        return ScanPattern(
            PatternMode.COMPLEX,
            Needles(None, None, effective_ci),
        )

    # 1. like — whole pattern is one pure literal
    if is_literal_string(body):
        return ScanPattern(
            PatternMode.LIKE,
            Needles(body, None, effective_ci),
        )

    # 2. or_simple — a|b|… or (a|b|…) with every branch pure
    pure_ors = _pure_or_branches(body)
    if pure_ors is not None:
        return ScanPattern(
            PatternMode.OR_SIMPLE,
            Needles(None, pure_ors, effective_ci),
        )

    # 3. affix_or — exactly one (…); prefix and/or suffix is pure literal
    grouped = _split_single_group(body)
    if grouped is not None:
        pre, inner, suf = grouped
        pre_ok = is_literal_string(pre) if pre else False
        suf_ok = is_literal_string(suf) if suf else False
        if pre_ok or suf_ok:
            req = _pick_longer(pre if pre_ok else None, suf if suf_ok else None)
            # or_group only when every inner branch is pure
            inner_ors = _pure_or_branches(inner)
            return ScanPattern(
                PatternMode.AFFIX_OR,
                Needles(req, inner_ors, effective_ci),
            )

    # 4. everything else
    return ScanPattern(
        PatternMode.COMPLEX,
        Needles(None, None, effective_ci),
    )
