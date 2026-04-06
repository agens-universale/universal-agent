"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import Dict, List


def _render_memory_files_section(memory_files: Dict[str, str]) -> str:
    """Render all memory files into an XML block for the summary message."""
    if not memory_files:
        return ""
    parts: List[str] = ["<memory_files>"]
    for name in sorted(memory_files):
        content = (memory_files[name] or "").strip()
        parts.append(f'<file name="{name}">')
        parts.append(content)
        parts.append("</file>")
    parts.append("</memory_files>")
    return "\n".join(parts)


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    memory_files: Dict[str, str] | None = None,
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата.

    Назначение: один каноничный формат «сводки» с зонами: текст сводки,
    memory files и последние шаги диалога.

    Args:
        summary_text: Основной текст суммаризации (сводка LLM).
        last_dialog_steps: Отформатированный хвост диалога.
        memory_files: Dict[str, str] с содержимым memory-файлов (может быть пустым).

    Returns:
        Единая строка с размеченными секциями.
    """
    lines: List[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")

    lines.append("")
    mf = memory_files or {}
    lines.append(_render_memory_files_section(mf))
    lines.append(
        "The memory_files section above contains all persistent memory registers for this thread. "
        "You do not need to call memory_files/read for this summarized context; "
        "append or update files as needed."
    )

    lines.append("")
    lines.append("<last_dialog_steps>")
    if last_dialog_steps.strip():
        lines.append(last_dialog_steps.strip())
    lines.append("</last_dialog_steps>")
    lines.append("")
    lines.append(
        "Continue the dialogue from here after summarization step. Summarization compresses history of dialogue — actively append "
        "durable decisions, constraints, URLs/paths, and verified facts to memory_files as the thread "
        "goes on so later turns stay consistent after future summarizations."
    )
    return "\n".join(lines)
