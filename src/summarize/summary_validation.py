"""Эвристики пригодности текста сводки для пайплайна суммаризации."""

INVALID_SUMMARY_PREFIXES = (
    "[tool_call ",
    "[assistant]",
    "[user]",
    "<dialog_summary>",
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


def validate_summary_text(text: str) -> tuple[bool, str]:
    """Проверяет качество/структуру ответа LLM для сводки диалога.

    Назначение: отсеять пустые, слишком короткие, похожие на мусор или недостаточно
    структурированные ответы до принятия их как `accumulated_summary`.

    Критерии: непустой текст; не начинается с известных «плохих» префиксов
    (`INVALID_SUMMARY_PREFIXES`); длина ≥ `MIN_SUMMARY_CHARS`; не меньше
    `MIN_SUMMARY_LINES` непустых строк; в верхнем регистре встречается не меньше
    трёх маркеров секций из `REQUIRED_SECTION_MARKERS` (гибкая проверка структуры).

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
    section_hits = 0
    upper_s = s.upper()
    for marker in REQUIRED_SECTION_MARKERS:
        if marker in upper_s:
            section_hits += 1
    if section_hits < 3:
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
    ok, _ = validate_summary_text(text)
    return ok
