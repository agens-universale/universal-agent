"""Образцы текста сводки для тестов (все секции из ``summary_spec``)."""

from summarize.summary_spec import section_titles


def valid_summary_text_for_tests(
    *,
    seed: str | None = None,
    line_filler: str | None = None,
) -> str:
    """Минимально валидная сводка: все обязательные секции, достаточная длина."""
    filler = line_filler or "- test content for validation padding.\n"
    parts: list[str] = []
    for title in section_titles():
        body = filler
        if title == "TASK_AND_CONTEXT" and seed:
            body = f"- {seed}\n{filler}"
        parts.append(f"{title}:\n{body}{filler}")
    return "\n\n".join(parts)
