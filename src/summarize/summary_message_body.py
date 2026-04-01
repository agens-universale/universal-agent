"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import List

SUGGESTED_MEMORY_HEADER = "## Suggested memory additions"


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    current_memory: str = "",
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата.

    Назначение: один каноничный формат «сводки» с тремя зонами: текст сводки,
    текущие заметки/память и последние шаги диалога, плюс инструкция не дублировать
    чтение `thread_notes`, если память уже передана в блоке.

    Args:
        summary_text: Основной текст суммаризации (может уже включать блок
            предложенной памяти от вызывающего кода).
        last_dialog_steps: Отформатированный хвост диалога (например из
            `format_last_dialog_steps` в модуле подготовки истории).
        current_memory: Строка текущего состояния `thread_notes` или пусто.

    Returns:
        Единая строка с размеченными секциями `<summary>`, `<thread_notes>`,
        `<last_dialog_steps>` и `<instruction>…</instruction>`.
    """
    lines: List[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")
    lines.append("")
    lines.append("<thread_notes>")
    lines.append((current_memory or "").strip())
    lines.append("</thread_notes>")
    lines.append("")
    lines.append("<last_dialog_steps>")
    if last_dialog_steps.strip():
        lines.append(last_dialog_steps.strip())
    lines.append("</last_dialog_steps>")
    lines.append("")
    lines.append(
        "<instruction>The thread_notes section above is the up-to-date state for this thread. "
        "You do not need to call thread_notes/read for this summarized context; append new durable facts to thread_notes when needed.</instruction>"
    )
    return "\n".join(lines)
