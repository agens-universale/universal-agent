"""Эвристики пригодности текста сводки для пайплайна суммаризации."""

from __future__ import annotations

import re

from summarize.summary_spec import MIN_SECTION_HITS, section_titles

INVALID_SUMMARY_PREFIXES = (
    "[tool_call ",
    "[assistant",
    "[user",
    # "<dialog_summary>",
)

MIN_SUMMARY_CHARS = 200
MIN_SUMMARY_LINES = 3
REQUIRED_SECTION_MARKERS = section_titles()

FALLBACK_SUMMARY = (
    "Automatic summarization produced no valid output. "
    "Continue from the latest confirmed user requirements and implementation state."
)

# Части имён секций после split по "_" не считаются ключевыми (союзы и т.п.),
# иначе «and» в обычном тексте засчитывал бы маркеры вроде TASK_AND_CONTEXT.
# «do»/«not» — иначе DO_NOT_REDO ловится по любому «not» в тексте.
_SECTION_KEYWORD_STOPWORDS = frozenset({"and", "do", "not"})


def _section_marker_hit(text: str, marker: str) -> bool:
    """Секция считается «есть», если в тексте как отдельное слово встречается
    хотя бы один фрагмент имени маркера (по подчёркиваниям), без учёта регистра.

    Примеры: для ``TASK_AND_CONTEXT`` достаточно ``task`` или ``context``;
    для ``GOALS`` — слово ``goals``. Фрагмент ``AND`` в имени маркера не проверяется.
    """
    keywords = [
        p
        for p in marker.split("_")
        if p and p.lower() not in _SECTION_KEYWORD_STOPWORDS
    ]
    if not keywords:
        return False
    lowered_keywords = [k.lower() for k in keywords]
    return any(
        re.search(rf"(?<!\\w){re.escape(kw)}(?!\\w)", text, re.IGNORECASE)
        for kw in lowered_keywords
    )


def section_hit_count(text: str) -> int:
    """Число секций из ``summary_spec``, для которых сработала эвристика."""
    s = (text or "").strip()
    return sum(1 for marker in REQUIRED_SECTION_MARKERS if _section_marker_hit(s, marker))


def missing_section_titles(text: str) -> tuple[str, ...]:
    """Заголовки секций из spec, которых нет в тексте сводки."""
    s = (text or "").strip()
    return tuple(m for m in REQUIRED_SECTION_MARKERS if not _section_marker_hit(s, m))


def validate_summary_text(text: str) -> tuple[bool, str]:
    """Проверяет качество/структуру ответа LLM для сводки диалога.

    Секции для подсчёта — ``summary_spec.section_titles()``; порог — ``MIN_SECTION_HITS``.

    Returns:
        Пара (успех, код причины): при успехе ``(True, "ok")``; иначе ``False`` и
        одно из: ``empty``, ``invalid_prefix``, ``too_short``, ``too_few_lines``,
        ``missing_structure``.
    """
    s = (text or "").strip()
    if not s:
        return False, "empty"
    if s.startswith(INVALID_SUMMARY_PREFIXES):
        return False, "invalid_prefix"
    if len(s) < MIN_SUMMARY_CHARS:
        return False, "too_short"
    non_empty_lines = [line for line in s.splitlines() if line.strip()]
    if len(non_empty_lines) < MIN_SUMMARY_LINES:
        return False, "too_few_lines"
    if section_hit_count(s) < MIN_SECTION_HITS:
        return False, "missing_structure"
    return True, "ok"


def is_valid_summary_text(text: str) -> bool:
    """Возвращает True, если черновик сводки проходит эвристическую валидацию."""
    status, _ = validate_summary_text(text)
    return status
