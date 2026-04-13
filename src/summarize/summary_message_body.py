"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import Dict, List

from xml.sax.saxutils import quoteattr

_JOURNAL_REGISTER = "journal"


def _register_meta(raw: str) -> tuple[int, int]:
    """Число символов и логических строк (как у splitlines) для метаданных."""
    text = raw or ""
    return len(text), len(text.splitlines())


def _render_memory_files_section(memory_files: Dict[str, str]) -> str:
    """Рендерит блок memory_files: journal целиком, остальные — индекс с метаданными.

    Journal соответствует регистру ``journal`` (как у инструмента ``memory_files``).
    Остальные ключи не встраиваются полностью — только имя, число символов и строк,
    чтобы модель при необходимости вызвала ``memory_files`` / ``read``.
    """
    if not memory_files:
        return ""
    parts: List[str] = ["<memory_files>"]

    if _JOURNAL_REGISTER in memory_files:
        content = (memory_files[_JOURNAL_REGISTER] or "").strip()
        parts.append(f'<file name="{_JOURNAL_REGISTER}">')
        parts.append(content)
        parts.append("</file>")

    others = sorted(name for name in memory_files if name != _JOURNAL_REGISTER)
    if others:
        parts.append("<other_registers>")
        parts.append(
            "These registers exist for this thread but are not inlined after summarization. "
            "When you need their full text, call the memory_files tool with action \"read\" "
            "and the corresponding file_name."
        )
        for name in others:
            chars, lines = _register_meta(memory_files[name] or "")
            parts.append(
                "<register "
                f"name={quoteattr(name)} chars={quoteattr(str(chars))} lines={quoteattr(str(lines))} "
                "/>"
            )
        parts.append("</other_registers>")

    parts.append("</memory_files>")
    return "\n".join(parts)


_MEMORY_FILES_HINT = (
    "The block above inlines the updated journal only. Other registers (if listed) are "
    "described by metadata; read them with memory_files when needed. "
    "Continue to append or update registers as the thread progresses."
)


def _render_user_messages_log_section(user_messages_log: List[str]) -> str:
    """Рендерит блок с журналом исходных пользовательских запросов."""
    if not user_messages_log:
        return ""
    parts: List[str] = ["<user_messages_log>"]
    for item in user_messages_log:
        text = str(item or "").strip()
        if text:
            parts.append(f"- {text}")
    parts.append("</user_messages_log>")
    return "\n".join(parts)


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    memory_files: Dict[str, str] | None = None,
    user_messages_log: List[str] | None = None,
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата.

    Назначение: один каноничный формат «сводки» с зонами: текст сводки,
    memory files (journal полностью после шага памяти, прочие — индекс) и последние шаги диалога.

    Args:
        summary_text: Основной текст суммаризации (сводка LLM).
        last_dialog_steps: Отформатированный хвост диалога.
        memory_files: Dict[str, str] с содержимым memory-файлов (может быть пустым).
        user_messages_log: Список "живых" пользовательских запросов (без техсообщений).

    Returns:
        Единая строка с размеченными секциями.
    """
    lines: List[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")

    lines.append("")
    mf = memory_files or {}
    mem_section = _render_memory_files_section(mf)
    if mem_section:
        lines.append(mem_section)
        lines.append(_MEMORY_FILES_HINT)

    user_log = user_messages_log or []
    user_log_section = _render_user_messages_log_section(user_log)
    if user_log_section:
        lines.append("")
        lines.append(user_log_section)
        lines.append(
            "Treat this block as the authoritative source of original user intent."
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
