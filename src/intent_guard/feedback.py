from __future__ import annotations

from intent_guard.validation_result import ValidationResult


def _bullet_block(title: str, items: list[str]) -> list[str]:
    cleaned = [str(x).strip() for x in items if str(x).strip()]
    if not cleaned:
        return []
    return [title, *[f"- {x}" for x in cleaned]]


def build_finish_gate_feedback(result: ValidationResult) -> str:
    """
    Текст reject finish gate → поле `message` в JSON `function_call_output` submit_solution.

    Не использовать сырой `feedback_for_agent` из JSON LLM — quality_gate §7.3.
    """
    action = result.action
    lines: list[str] = [f"Finish gate: {action}."]

    if action == "accept":
        return lines[0]

    if action == "fix_report":
        lines.append(
            "По trace запрос уже закрыт; исправь аргументы submit_solution без повторения "
            "уже успешных шагов из trace."
        )
        lines.extend(_bullet_block("Отчёт не соответствует trace или intent:", result.report_mismatches))
        if not result.report_mismatches:
            lines.extend(_bullet_block("Уточнения (conflicts):", result.conflicts))
        lines.append(
            "Проверь solution_type, task_report, handoff, comment так, чтобы они совпали с тем, "
            "что видно в trace."
        )
        return "\n".join(lines)

    if action == "continue_work":
        lines.append("По trace недостаточно фактов под current_user_requests; нужно доработать работу.")
        lines.extend(_bullet_block("Пробелы по trace:", result.trace_gaps))
        if not result.trace_gaps:
            lines.extend(_bullet_block("Дополнительно:", result.missing_requirements))
        lines.append(
            "Сделай недостающий шаг (нужный tool), затем снова вызови submit_solution с честным отчётом."
        )
        return "\n".join(lines)

    if action == "fix_report_and_continue":
        lines.append("Сначала закрой пробелы по trace, затем приведи submit_solution к фактам trace.")
        lines.extend(_bullet_block("Пробелы по trace:", result.trace_gaps))
        if not result.trace_gaps:
            lines.extend(_bullet_block("Дополнительно:", result.missing_requirements))
        lines.extend(_bullet_block("Отчёт не соответствует:", result.report_mismatches))
        if not result.report_mismatches:
            lines.extend(_bullet_block("Уточнения (conflicts):", result.conflicts))
        lines.append(
            "Не повторяй шаги, которые уже видны как успешные в trace."
        )
        return "\n".join(lines)

    lines.append(f"Неизвестный action={action!r}; исправь submit_solution или продолжи работу.")
    return "\n".join(lines)


def build_finish_gate_reject_technical_user_content(feedback: str) -> str:
    """Technical user message after finish gate reject — continue the open turn."""
    body = str(feedback or "").strip()
    lines = [
        "submit_solution отклонён quality gate: ответ не соответствует интенту пользователя.",
        "Продолжай работу над текущим ходом; не считай ход завершённым.",
    ]
    if body:
        lines.extend(["", "Детали:", body])
    return "\n".join(lines)
