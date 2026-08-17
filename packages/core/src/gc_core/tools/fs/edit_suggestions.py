"""Nearest-match suggestions for exact edit/search misses (diagnostics only)."""

from __future__ import annotations

import difflib
from typing import Any


def _normalize_spaces(text: str) -> str:
    return " ".join(text.split())


def _truncate_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[:max_chars] + "…"


def build_edit_miss_suggestions(
    search: str,
    content: str,
    *,
    max_suggestions: int = 3,
    min_score: float = 0.2,
    max_lines: int = 5000,
    max_text_chars: int = 200,
) -> tuple[list[dict[str, Any]], int]:
    """Return (top suggestions, total candidates above min_score)."""
    if not search or not content:
        return [], 0

    lines = content.splitlines()
    limited_lines = lines[:max_lines]
    if not limited_lines:
        return [], 0

    is_multiline_search = "\n" in search or "\r" in search
    if is_multiline_search:
        return _multiline_suggestions(
            search=search,
            limited_lines=limited_lines,
            max_suggestions=max_suggestions,
            min_score=min_score,
            max_text_chars=max_text_chars,
        )
    return _single_line_suggestions(
        search=search,
        limited_lines=limited_lines,
        max_suggestions=max_suggestions,
        min_score=min_score,
        max_text_chars=max_text_chars,
    )


def _multiline_suggestions(
    *,
    search: str,
    limited_lines: list[str],
    max_suggestions: int,
    min_score: float,
    max_text_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    search_lines = search.splitlines()
    if not search_lines or len(search_lines) > len(limited_lines):
        return [], 0

    normalized_search = "\n".join(
        _normalize_spaces(line).lower().strip() for line in search_lines
    )
    if not normalized_search.strip():
        return [], 0

    scored: list[tuple[float, int, str]] = []
    window_size = len(search_lines)
    for start_idx in range(0, len(limited_lines) - window_size + 1):
        window_lines = limited_lines[start_idx : start_idx + window_size]
        normalized_window = "\n".join(
            _normalize_spaces(line).lower().strip() for line in window_lines
        )
        score = difflib.SequenceMatcher(
            None, normalized_search, normalized_window
        ).ratio()
        if score <= min_score:
            continue
        scored.append((score, start_idx + 1, "\n".join(window_lines)))

    scored.sort(key=lambda item: item[0], reverse=True)
    suggestions_total = len(scored)
    suggestions: list[dict[str, Any]] = []
    for score, start_line, text in scored[:max_suggestions]:
        suggestions.append(
            {
                "line": start_line,
                "end_line": start_line + window_size - 1,
                "text": _truncate_text(text, max_text_chars),
                "reason": "multiline difflib similarity",
                "score": round(score, 4),
            }
        )
    return suggestions, suggestions_total


def _single_line_suggestions(
    *,
    search: str,
    limited_lines: list[str],
    max_suggestions: int,
    min_score: float,
    max_text_chars: int,
) -> tuple[list[dict[str, Any]], int]:
    normalized_search = _normalize_spaces(search).lower().strip()
    if not normalized_search:
        return [], 0

    scored: list[tuple[float, int, str]] = []
    for line_no, line in enumerate(limited_lines, start=1):
        normalized_line = _normalize_spaces(line).lower().strip()
        if not normalized_line:
            continue
        score = difflib.SequenceMatcher(
            None, normalized_search, normalized_line
        ).ratio()
        if score <= min_score:
            continue
        scored.append((score, line_no, line))

    scored.sort(key=lambda item: item[0], reverse=True)
    suggestions_total = len(scored)
    suggestions: list[dict[str, Any]] = []
    for score, line_no, text in scored[:max_suggestions]:
        suggestions.append(
            {
                "line": line_no,
                "text": _truncate_text(text, max_text_chars),
                "reason": "single-line difflib similarity",
                "score": round(score, 4),
            }
        )
    return suggestions, suggestions_total
