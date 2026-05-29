"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import List

_WORKING_MEMORY_HINT = (
    "The block above inlines the current working_memory snapshot for this thread. "
    "Prefer this snapshot when present; use working_memory tool only if you need "
    "content updated after this summary. Continue to append durable deltas as the "
    "thread progresses."
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
    working_memory_block: str = "",
    user_intent_log: List[str] | None = None,
    current_user_intent: List[str] | None = None,
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата."""
    lines: List[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")

    wm_block = str(working_memory_block or "").strip()
    if wm_block:
        lines.append("")
        lines.append(wm_block)
        lines.append(_WORKING_MEMORY_HINT)

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
        "Continue the dialogue from here after summarization step. Summarization compresses "
        "history of dialogue — actively append durable decisions, constraints, URLs/paths, "
        "and verified facts to working_memory as the thread goes on so later turns stay "
        "consistent after future summarizations."
    )
    return "\n".join(lines)
