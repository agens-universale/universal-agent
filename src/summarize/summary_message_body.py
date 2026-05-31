"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import Any, Mapping

_WORKING_MEMORY_HINT = (
    "The block above inlines the current working_memory snapshot for this thread. "
    "Prefer this snapshot when present; use working_memory tool only if you need "
    "content updated after this summary. Continue to append durable deltas as the "
    "thread progresses."
)


def _bool_attr(value: bool) -> str:
    return "true" if value else "false"


def render_user_intent_log_block(preview: Mapping[str, Any]) -> str:
    """Closed log tail slice with read_user_intent_log-compatible attributes."""
    total_records = int(preview.get("total_records") or 0)
    content = str(preview.get("content") or "")
    if total_records <= 0 and not content.strip():
        return ""
    attrs = (
        f'total_chars="{int(preview.get("total_chars") or 0)}" '
        f'total_records="{total_records}" '
        f'offset="{int(preview.get("offset") or 0)}" '
        f'limit="{int(preview.get("limit") or 0)}" '
        f'truncated="{_bool_attr(bool(preview.get("truncated")))}">'
    )
    body = content.rstrip("\n")
    return f"<user_intent_log {attrs}\n{body}\n</user_intent_log>"


def render_current_user_intent_block(current_jsonl: str) -> str:
    """Full JSONL of the open user turn (no pagination)."""
    body = str(current_jsonl or "").rstrip("\n")
    if not body.strip():
        return ""
    return f"<current_user_intent>\n{body}\n</current_user_intent>"


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    working_memory_block: str = "",
    user_intent_log_preview: Mapping[str, Any] | None = None,
    current_user_intent_jsonl: str = "",
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата."""
    lines: list[str] = ["<summary>"]
    lines.append(summary_text.strip())
    lines.append("</summary>")

    wm_block = str(working_memory_block or "").strip()
    if wm_block:
        lines.append("")
        lines.append(wm_block)
        lines.append(_WORKING_MEMORY_HINT)

    if user_intent_log_preview is not None:
        log_section = render_user_intent_log_block(user_intent_log_preview)
        if log_section:
            lines.append("")
            lines.append(log_section)

    current_section = render_current_user_intent_block(current_user_intent_jsonl)
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
