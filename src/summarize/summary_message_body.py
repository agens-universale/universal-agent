"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import Dict, List

from xml.sax.saxutils import quoteattr

def _render_memory_files_section(memory_files: Dict[str, str]) -> str:
    """Рендерит блок memory_files: все непустые регистры inline в ``<file>`` (D17)."""
    if not memory_files:
        return ""
    parts: List[str] = ["<memory_files>"]
    for name in sorted(memory_files):
        content = (memory_files.get(name) or "").strip()
        if not content:
            continue
        parts.append(f"<file name={quoteattr(name)}>")
        parts.append(content)
        parts.append("</file>")
    parts.append("</memory_files>")
    return "\n".join(parts)


_MEMORY_FILES_HINT = (
    "The block above inlines all non-empty thread wiki registers from the latest snapshot. "
    "Prefer this snapshot when present; use memory_files read only if you need content "
    "updated after this summary. Continue to append or update registers as the thread progresses."
)

_USER_INTENT_LOG_HINT = (
    "Prior completed user turns in this thread. "
    "Does not include the open turn (see <current_user_intent>). "
    "The user may repeat similar wording in a later turn."
)

_CURRENT_USER_INTENT_HINT = (
    "Open user turn: what the user wants in the current step, including clarifications "
    "from the ongoing exchange."
)


def _render_intent_lines_section(tag: str, lines: List[str], *, hint: str) -> str:
    """Рендерит блок списка intent-строк с подсказкой под тегом."""
    items = [str(item or "").strip() for item in lines]
    items = [item for item in items if item]
    if not items:
        return ""
    parts: List[str] = [f"<{tag}>"]
    for text in items:
        parts.append(f"- {text}")
    parts.append(f"</{tag}>")
    parts.append(hint)
    return "\n".join(parts)


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    memory_files: Dict[str, str] | None = None,
    user_intent_log: List[str] | None = None,
    current_user_intent: List[str] | None = None,
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата.

    Назначение: один каноничный формат «сводки» с зонами: текст сводки,
    memory files (journal полностью после шага памяти, прочие — индекс) и последние шаги диалога.

    Args:
        summary_text: Основной текст суммаризации (сводка LLM).
        last_dialog_steps: Отформатированный хвост диалога.
        memory_files: Dict[str, str] с содержимым memory-файлов (может быть пустым).
        user_intent_log: Закрытые user turn'ы (flush после accept submit_solution).
        current_user_intent: Открытый user turn (до commit).

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

    log_section = _render_intent_lines_section(
        "user_intent_log",
        user_intent_log or [],
        hint=_USER_INTENT_LOG_HINT,
    )
    current_section = _render_intent_lines_section(
        "current_user_intent",
        current_user_intent or [],
        hint=_CURRENT_USER_INTENT_HINT,
    )
    if log_section:
        lines.append("")
        lines.append(log_section)
    if current_section:
        lines.append("")
        lines.append(current_section)

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
