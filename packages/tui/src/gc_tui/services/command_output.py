"""Вывод и текст команды для блока `Ran` (спека P4 §4.0, §4.1) — чистый.

Конверт exec-тула (`exec_command`, `exec_python`, `skills_run_script`) →
`CommandOutput`: строки `stdout`, затем `stderr` (порядок чередования
каналов неизвестен), превью и счёт скрытого. Текст команды — один рендер
для всех источников: сырые аргументы вызова построчно, без `cd <корень> &&`.
"""

from __future__ import annotations

from typing import Any, Optional

from gc_core.constants.tool_names import (
    EXEC_COMMAND_NAME,
    EXEC_PYTHON_NAME,
    SKILLS_RUN_SCRIPT_NAME,
)
from gc_core.execution.progress import run_command_text, strip_root_cd
from rich.text import Text

from gc_tui.models import CommandOutput
from gc_tui.services.plural import plural_form
from gc_tui.services.tool_blocks import OUTPUT_PREVIEW_LINES
from gc_tui.services.tool_preview import unwrap_tool_output_dict

#: Конверты exec-тулов по ключу `tool`: где лежат каналы и код — в самом
#: конверте (`None`) или под ключом.
_BODY_OF: dict[str, Optional[str]] = {
    EXEC_COMMAND_NAME: None,
    EXEC_PYTHON_NAME: None,
    SKILLS_RUN_SCRIPT_NAME: "result",
}


def omitted_line(chars: int) -> str:
    """Строка пропуска между головой и хвостом обрезанного канала."""
    word = plural_form(chars, "символ", "символа", "символов")
    return f"… пропущено {chars:,} {word}".replace(",", " ")


def clean_output_line(text: str) -> str:
    """Строка вывода как на экране: без `\\r` в конце и без ANSI."""
    return Text.from_ansi(text.rstrip("\r")).plain


def _split(text: Any) -> list[str]:
    if not isinstance(text, str) or not text:
        return []
    lines = text.replace("\r\n", "\n").split("\n")
    return [clean_output_line(line) for line in lines]


def _drop_blank_tail(lines: list[str]) -> list[str]:
    while lines and not lines[-1].strip():
        lines.pop()
    return lines


def _channel(data: dict) -> tuple[list[str], int, Optional[int]]:
    """Строки канала, пропущено символов и место строки пропуска."""
    if not data.get("truncated"):
        return _drop_blank_tail(_split(data.get("content"))), 0, None
    head = _split(data.get("head"))
    tail = _drop_blank_tail(_split(data.get("tail")))
    if head and not head[-1]:
        head.pop()
    omitted = data.get("omitted_chars")
    omitted = omitted if isinstance(omitted, int) else 0
    return head + tail, omitted, len(head)


def _exit_code(data: dict) -> Optional[int]:
    for key in ("exit_code", "return_code"):
        value = data.get(key)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _exec_body(output: Any) -> Optional[dict]:
    """Каналы и код конверта exec-тула; конверт узнаётся по `tool`."""
    data = unwrap_tool_output_dict(output if isinstance(output, str) else "")
    tool = data.get("tool") if data else None
    if not isinstance(tool, str) or tool not in _BODY_OF:
        return None
    key = _BODY_OF[tool]
    body = data if key is None else data.get(key)
    if not isinstance(body, dict) or not all(
        isinstance(body.get(name), dict) for name in ("stdout", "stderr")
    ):
        return None
    return body


def parse_command_output(output: Any) -> Optional[CommandOutput]:
    """Конверт exec-тула → `CommandOutput`; не конверт exec → `None`."""
    data = _exec_body(output)
    if data is None:
        return None
    content: list[str] = []
    lines: list[str] = []
    omitted_total = 0
    for key in ("stdout", "stderr"):
        channel, omitted, marker_at = _channel(data[key])
        content += channel
        if marker_at is not None:
            channel.insert(marker_at, omitted_line(omitted))
        lines += channel
        omitted_total += omitted
    preview = tuple(content[:OUTPUT_PREVIEW_LINES])
    return CommandOutput(
        lines=tuple(lines),
        omitted_chars=omitted_total,
        exit_code=_exit_code(data),
        timed_out=bool(data.get("timed_out")),
        background=isinstance(data.get("background"), dict),
        preview=preview,
        hidden=len(content) - len(preview),
    )


def command_lines(
    wire_name: Optional[str], arguments: Any, *, root: Optional[str]
) -> tuple[str, ...]:
    """Сырой текст Run-тула построчно, без `cd <корень> &&` и пустых
    хвостовых строк; не Run-тул или аргументов нет — `()`. Ширину не режет:
    это делает строка."""
    return split_command(run_command_text(wire_name, arguments), root=root)


def split_command(
    text: Optional[str], *, root: Optional[str]
) -> tuple[str, ...]:
    """Сырой текст команды построчно, без `cd <корень> &&` и пустых
    хвостовых строк; текста нет — `()`."""
    if not text:
        return ()
    if root:
        text = strip_root_cd(text, root)
    lines = [line.rstrip("\r") for line in text.split("\n")]
    return tuple(_drop_blank_tail(lines))
