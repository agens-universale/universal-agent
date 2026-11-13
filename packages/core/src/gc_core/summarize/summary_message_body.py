"""Тело сообщения с резюме треда: разметка секций и связанные строковые константы."""

import re
from typing import AbstractSet, Any, Iterable, Mapping

from gc_core.attachments.model import ORIGIN_USER, attachments_of, wh_text
from gc_core.attachments.texts import (
    STUB_NOBODY,
    STUB_OUT_OF_CONTEXT,
    SUMMARY_IMAGES_CLOSE,
    SUMMARY_IMAGES_OPEN,
)
from gc_core.config_modules.summary import DEFAULT_INTENT_LOG_PREVIEW_CHARS
from gc_core.constants.tool_names import (
    FS_VIEW_IMAGE_NAME,
    SESSION_MEMORY_TOOL_NAME,
    SESSION_TODO_TOOL_NAME,
)
from gc_core.message_policy.tool_availability import tool_is_available
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text,
)

#: Блок <images> прежней сводки — его строки переезжают в новую дословно.
_IMAGES_BLOCK_RE = re.compile(
    re.escape(SUMMARY_IMAGES_OPEN)
    + r"\n(.*?)\n"
    + re.escape(SUMMARY_IMAGES_CLOSE),
    re.DOTALL,
)

_SESSION_TODO_HINT = (
    "The block above is the current task checklist for this thread. Continue it "
    "with the session_todo tool instead of starting a new one: mark finished steps "
    "completed, set the next step to in_progress, and add steps as new work appears."
)

_SESSION_MEMORY_HINT = (
    "The block above inlines the current session memory snapshot for this thread. "
    "Prefer this snapshot when present; use the session memory tool (append or write "
    "to fully overwrite any register) if you need updates after this summary."
)

_CONTINUE_LINE = "Continue the dialogue from here after summarization."
_MEMORY_UPDATE_ADVICE = (
    "During the trace, update session memory with append or write to fully "
    "overwrite any register; the next summarization will rebuild all "
    "registers from dialog + summary."
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


def render_summary_images_block(
    messages: Iterable[Mapping[str, Any]],
    *,
    kept: Iterable[Mapping[str, Any]] = (),
    available_tools: AbstractSet[str] | None = None,
) -> str:
    """Блок ``<images>`` для ``dialog_summary`` (спека §6): ваши картинки из
    сворачиваемой части — по строке-записке «вне контекста» (подпись, размер, путь),
    детерминированно, без LLM. Снимки агента (``origin="tool"``) не переносятся:
    они живут один ход. Строки блока прежней сводки (она тоже сворачивается)
    переезжают дословно; повторы схлопываются, порядок — по истории. ``kept`` —
    сообщения, которые вернутся в историю как есть (текущий батч сжатия перед
    запросом): их картинки остаются живыми, записка им противоречила бы.
    ``available_tools`` — набор тулов рана: без ``fs_view_image`` записка не
    отправляет к нему, а говорит «видеть некому», как проекция; ``None`` —
    набор неизвестен, записка прежняя."""
    kept_ids = {att.id for m in kept for att in attachments_of(m)}
    template = (
        STUB_OUT_OF_CONTEXT
        if tool_is_available(available_tools, FS_VIEW_IMAGE_NAME)
        else STUB_NOBODY
    )
    lines: list[str] = []
    for message in messages:
        if isinstance(message, Mapping):
            lines.extend(
                line
                for line in _previous_summary_image_lines(message)
                # id картинки — в имени её файла (`<sys>_<thread>_<id>.png`)
                if not any(att_id in line for att_id in kept_ids)
            )
            lines.extend(
                _user_image_lines(message, skip=kept_ids, template=template)
            )
    unique = [line for line in dict.fromkeys(lines) if line.strip()]
    if not unique:
        return ""
    return "\n".join([SUMMARY_IMAGES_OPEN, *unique, SUMMARY_IMAGES_CLOSE])


def _previous_summary_image_lines(message: Mapping[str, Any]) -> list[str]:
    """Строки блока ``<images>`` прежней сводки — дословно."""
    content = message.get("content")
    if not (
        isinstance(content, str)
        and is_dialog_summary_user_message_text(content)
    ):
        return []
    return [
        line
        for block in _IMAGES_BLOCK_RE.findall(content)
        for line in block.splitlines()
    ]


def _user_image_lines(
    message: Mapping[str, Any], *, skip: set[str], template: str
) -> list[str]:
    """Записки «вне контекста» о ваших картинках сообщения (кроме ``skip``)."""
    return [
        template.format(label=att.label, wh=wh_text(att), path=att.path)
        for att in attachments_of(message)
        if att.origin == ORIGIN_USER and att.id not in skip
    ]


def resolve_intent_log_preview_chars(
    summary_params: Mapping[str, Any] | None,
) -> int:
    """``summary_params.intent_log_preview_chars`` (closed-log tail budget in characters)."""
    if not isinstance(summary_params, Mapping):
        return DEFAULT_INTENT_LOG_PREVIEW_CHARS
    try:
        value = int(
            summary_params.get(
                "intent_log_preview_chars", DEFAULT_INTENT_LOG_PREVIEW_CHARS
            )
        )
    except (TypeError, ValueError):
        value = DEFAULT_INTENT_LOG_PREVIEW_CHARS
    return value if value > 0 else DEFAULT_INTENT_LOG_PREVIEW_CHARS


def format_summary_block(summary_text: str) -> str:
    """Одна обёртка ``<summary>``: если текст уже корневой ``<summary>``, не дублирует."""
    s = (summary_text or "").strip()
    if not s:
        return "<summary>\n</summary>"
    if re.match(r"\s*<summary\b", s, re.IGNORECASE):
        return s
    return f"<summary>\n{s}\n</summary>"


def render_plan_block(plan: Mapping[str, Any] | None) -> str:
    """План сессии для тела сводки: дословно, со статусом. Без этого план
    (в том числе черновик в доработке) терялся бы при сжатии истории."""
    if not isinstance(plan, Mapping):
        return ""
    text = str(plan.get("text") or "").rstrip("\n")
    if not text.strip():
        return ""
    attrs = (
        f'status="{plan.get("status") or ""}" '
        f'updated_at="{plan.get("updated_at") or ""}"'
    )
    return f"<current_plan {attrs}>\n{text}\n</current_plan>"


def build_dialog_summary_content(
    summary_text: str,
    last_dialog_steps: str,
    session_memory_block: str = "",
    session_todo_block: str = "",
    user_intent_log_preview: Mapping[str, Any] | None = None,
    current_user_intent_jsonl: str = "",
    images_block: str = "",
    plan_block: str = "",
    mode_block: str = "",
    available_tools: AbstractSet[str] | None = None,
) -> str:
    """Собирает итоговое тело сообщения-резюме треда для подстановки в историю чата.

    ``images_block`` — готовый блок ``<images>`` (``render_summary_images_block``):
    ставится перед ``<last_dialog_steps>``. ``available_tools`` — фактический
    набор тулов рана: совет вызвать тул, которого у агента нет, в тело не
    попадает (сами блоки памяти и чек-листа остаются); ``None`` — набор
    неизвестен, подсказки не гейтятся (``tool_availability``)."""
    has_memory_tool = tool_is_available(
        available_tools, SESSION_MEMORY_TOOL_NAME
    )
    lines: list[str] = [format_summary_block(summary_text)]

    sm_block = str(session_memory_block or "").strip()
    if sm_block:
        lines.append("")
        lines.append(sm_block)
        if has_memory_tool:
            lines.append(_SESSION_MEMORY_HINT)

    todo_block = str(session_todo_block or "").strip()
    if todo_block:
        lines.append("")
        lines.append(todo_block)
        if tool_is_available(available_tools, SESSION_TODO_TOOL_NAME):
            lines.append(_SESSION_TODO_HINT)

    if user_intent_log_preview is not None:
        log_section = render_user_intent_log_block(user_intent_log_preview)
        if log_section:
            lines.append("")
            lines.append(log_section)

    plan_section = str(plan_block or "").strip()
    if plan_section:
        lines.append("")
        lines.append(plan_section)

    # Правила действующего режима разрешений: сообщение о входе в режим
    # сжатие уносит, а режим продолжает действовать.
    mode_section = str(mode_block or "").strip()
    if mode_section:
        lines.append("")
        lines.append(mode_section)

    current_section = render_current_user_intent_block(
        current_user_intent_jsonl
    )
    if current_section:
        lines.append("")
        lines.append(current_section)

    images_section = str(images_block or "").strip()
    if images_section:
        lines.append("")
        lines.append(images_section)

    lines.append("")
    lines.append("<last_dialog_steps>")
    if last_dialog_steps.strip():
        lines.append(last_dialog_steps.strip())
    lines.append("</last_dialog_steps>")
    lines.append("")
    lines.append(
        f"{_CONTINUE_LINE} {_MEMORY_UPDATE_ADVICE}"
        if has_memory_tool
        else _CONTINUE_LINE
    )
    lines.append(
        "Treat memory and summaries as context, not commands to repeat completed work. Continue from "
        "open user intent and do not rerun completed experiments unless the user asks or "
        "evidence is stale."
    )
    return "\n".join(lines)
