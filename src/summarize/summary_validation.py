"""Эвристики пригодности текста сводки для пайплайна суммаризации."""

import re

INVALID_SUMMARY_PREFIXES = (
    "[tool_call ",
    "[assistant",
    "[user",
    # "<dialog_summary>",
)

MIN_SUMMARY_CHARS = 200
MIN_SUMMARY_LINES = 3
REQUIRED_SECTION_MARKERS = (
    "TASK_AND_CONTEXT",
    "GOALS",
    "PLAN_AND_PROGRESS",
    "NEXT_ACTIONS",
    "SPEC_AND_CONSTRAINTS",
    "RISKS_AND_OPEN_QUESTIONS",
    "IMPORTANT_ARTIFACTS",
)
FALLBACK_SUMMARY = (
    "Automatic summarization produced no valid output. "
    "Continue from the latest confirmed user requirements and implementation state."
)

_MIN_SECTION_HITS = 2
# Части имён секций после split по "_" не считаются ключевыми (союзы и т.п.),
# иначе «and» в обычном тексте засчитывал бы маркеры вроде TASK_AND_CONTEXT.
_SECTION_KEYWORD_STOPWORDS = frozenset({"and"})


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


def validate_summary_text(text: str) -> tuple[bool, str]:
    """Проверяет качество/структуру ответа LLM для сводки диалога.

    Назначение: отсеять пустые, слишком короткие, похожие на мусор или недостаточно
    структурированные ответы до принятия их как `accumulated_summary`.

    Критерии: непустой текст; не начинается с известных «плохих» префиксов
    (`INVALID_SUMMARY_PREFIXES`); длина ≥ `MIN_SUMMARY_CHARS`; не меньше
    `MIN_SUMMARY_LINES` непустых строк; не менее ``_MIN_SECTION_HITS`` «попаданий»
    по секциям из `REQUIRED_SECTION_MARKERS` — для каждого маркера достаточно,
    чтобы в тексте как отдельное слово встретился хотя бы один его фрагмент
    (части имени по ``_``, без учёта регистра; союз ``and`` из имени не используется),
    например ``Task and context`` закрывает ``TASK_AND_CONTEXT``.

    Args:
        text: Строка сводки от модели.

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
    section_hits = sum(
        1 for marker in REQUIRED_SECTION_MARKERS if _section_marker_hit(s, marker)
    )
    if section_hits < _MIN_SECTION_HITS:
        return False, "missing_structure"
    return True, "ok"


def is_valid_summary_text(text: str) -> bool:
    """Возвращает True, если черновик сводки проходит эвристическую валидацию.

    Назначение: быстрая проверка без разбора кода причины отказа; детали см.
    в `validate_summary_text`.

    Args:
        text: Ответ модели с текстом сводки.

    Returns:
        ``True``/``False`` по результату валидации.
    """
    status, _ = validate_summary_text(text)
    return status
