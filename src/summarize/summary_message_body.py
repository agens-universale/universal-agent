"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

from typing import Any, Iterable, Mapping

from config_modules.summary import DEFAULT_INTENT_LOG_PREVIEW_CHARS
from intent_log import serialize, tail_preview
from intent_log.record import IntentRecord

_WORKING_MEMORY_HINT = (
    "The block above inlines the current working_memory snapshot for this thread. "
    "Prefer this snapshot when present; use working_memory tool only if you need "
    "content updated after this summary. Continue to append durable deltas as the "
    "thread progresses."
)


def _bool_attr(value: bool) -> str:
    return "true" if value else "false"


def render_user_intent_log_block(preview: Mapping[str, Any]) -> str:
    """Closed log tail (from :func:`intent_log.tail_preview`) for dialog_summary.

    ``preview`` is the dict from ``tail_preview`` / ``read_page``: ``offset`` and
    ``limit`` are **record** index and count; ``total_chars`` is the full closed log.
    Char budget for the tail comes from ``summary.intent_log_preview_chars``, not
    from ``intent_scope.read_page_record_limit``.
    """
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


def resolve_intent_log_preview_chars(summary_params: Mapping[str, Any] | None) -> int:
    """``summary_params.intent_log_preview_chars`` (closed-log tail budget in characters)."""
    if not isinstance(summary_params, Mapping):
        return DEFAULT_INTENT_LOG_PREVIEW_CHARS
    try:
        value = int(
            summary_params.get("intent_log_preview_chars", DEFAULT_INTENT_LOG_PREVIEW_CHARS)
        )
    except (TypeError, ValueError):
        value = DEFAULT_INTENT_LOG_PREVIEW_CHARS
    return value if value > 0 else DEFAULT_INTENT_LOG_PREVIEW_CHARS


def build_intent_blocks_prefix(
    closed_records: Iterable[IntentRecord],
    open_records: Iterable[IntentRecord],
    *,
    intent_log_preview_chars: int,
) -> str:
    """XML intent blocks for summary LLM (same wire format as ``dialog_summary``).

    Closed log: ``tail_preview`` (whole records within char budget). Open turn: full JSONL.
    """
    safe_chars = (
        int(intent_log_preview_chars)
        if int(intent_log_preview_chars) > 0
        else DEFAULT_INTENT_LOG_PREVIEW_CHARS
    )
    preview = tail_preview(list(closed_records), safe_chars)
    parts: list[str] = []
    log_block = render_user_intent_log_block(preview)
    if log_block:
        parts.append(log_block)
    current_block = render_current_user_intent_block(serialize(list(open_records)))
    if current_block:
        parts.append(current_block)
    if not parts:
        return ""
    return "\n\n".join(parts) + "\n\n"


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
