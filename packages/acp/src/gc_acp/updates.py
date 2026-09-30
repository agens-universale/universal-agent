"""Дельты хода и история треда → session/update."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.message_projection import project_assistant_text
from gc_core.tools.turn.submit_solution import SubmitSolution

from gc_acp.tool_flow import report_from_record, report_from_sidecar

_ANSWER = "agent_message_chunk"
_THINK = "agent_thought_chunk"
_USER = "user_message_chunk"
_SKIP_TYPES = frozenset(
    {
        "function_call",
        "tool_call",
        "function_call_output",
        "tool_call_output",
    }
)
_ROLE_KIND = {"user": _USER, "assistant": _ANSWER}


def new_message_id() -> str:
    return str(uuid.uuid4())


def text_chunk(session_update: str, text: str, message_id: str) -> dict:
    return {
        "sessionUpdate": session_update,
        "content": {"type": "text", "text": text},
        "messageId": message_id,
    }


class ChunkAssembler:
    """Один messageId на непрерывную серию чанков одного вида."""

    def __init__(self) -> None:
        self._kind: str | None = None
        self._message_id: str | None = None
        self._answer = ""

    @property
    def answer(self) -> str:
        return self._answer

    def feed(self, kind: str, text: str) -> list[dict]:
        if kind == "reset":
            self._kind = None
            self._message_id = None
            return []
        if kind not in ("answer", "think"):
            return []
        if not isinstance(text, str) or text == "":
            return []
        self._continue(kind)
        if kind == "answer":
            self._answer += text
        update = _ANSWER if kind == "answer" else _THINK
        return [text_chunk(update, text, self._message_id or "")]

    def flush_answer(self, last_answer: Any) -> list[dict]:
        """Дослать прозу, которой не было в answer-чанках этого хода."""
        extra = unseen_answer(self._answer, last_answer)
        if extra == "":
            return []
        if self._kind != "answer":
            self._kind = "answer"
            self._message_id = new_message_id()
        self._answer += extra
        return [text_chunk(_ANSWER, extra, self._message_id or "")]

    def _continue(self, kind: str) -> None:
        if kind == self._kind and self._message_id is not None:
            return
        self._kind = kind
        self._message_id = new_message_id()


def unseen_answer(sent: str, final: Any) -> str:
    if not isinstance(final, str) or final == "":
        return ""
    if final.startswith(sent):
        return final[len(sent) :]
    if final in sent:
        return ""
    return final


def history_updates(
    messages: list[Any],
    *,
    subagent_records: list[Any] | None = None,
) -> list[dict]:
    """Проигрыш треда: у каждого сообщения свой messageId.

    function_call встаёт карточкой между прозой. Результат тула — смена
    статуса той же карточки, не текст ответа. Принятый turn_finish
    становится отчётом; отклонённая попытка и вердикт судьи не показываются.
    Служебные user-сообщения ядра (сводка, чек-лист, technical) в ленту
    не идут — та же ручка, что у заголовка сессии.

    Вызовы из sidecar субагента встают сразу после карточки этого субагента,
    до его результата: для клиента это такие же действия, как тулы родителя.
    """
    records = subagent_records or []
    replay = _Replay(
        seen=set(),
        grouped=_records_by_parent(records),
        rejected=_rejected_finish_ids(messages),
    )
    for message in messages:
        _append_history_message(replay, message)
    replay.rendered.extend(
        _leftover_tool_updates(records, replay.consumed, replay.seen)
    )
    return replay.rendered


@dataclass
class _Replay:
    seen: set[str]
    grouped: dict[str, list[dict]]
    rejected: set[str]
    consumed: set[int] = field(default_factory=set)
    stack: set[str] = field(default_factory=set)
    rendered: list[dict] = field(default_factory=list)
    last_agent: str = ""


def _append_history_message(replay: _Replay, message: Any) -> None:
    tool = _tool_report(message, replay.seen)
    if tool is not None:
        _append_tool(replay, tool)
        return
    if _is_finish_call(message):
        _append_finish(replay, message)
        return
    _append_prose(replay, message)


def _append_tool(replay: _Replay, tool: dict) -> None:
    replay.rendered.append(tool)
    if tool.get("sessionUpdate") != "tool_call":
        return
    call_id = tool.get("toolCallId")
    if not isinstance(call_id, str) or call_id == "":
        return
    replay.rendered.extend(
        _nested_tool_updates(
            call_id,
            replay.grouped,
            replay.seen,
            replay.consumed,
            replay.stack,
        )
    )


def _append_finish(replay: _Replay, message: Any) -> None:
    text = _accepted_finish_text(message, replay.rejected, replay.last_agent)
    if text == "":
        return
    replay.rendered.append(text_chunk(_ANSWER, text, new_message_id()))
    replay.last_agent = text


def _append_prose(replay: _Replay, message: Any) -> None:
    chunk = _history_chunk(message)
    if chunk is None:
        return
    replay.rendered.append(chunk)
    if chunk["sessionUpdate"] == _ANSWER:
        replay.last_agent = chunk["content"]["text"]


def _is_finish_call(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "function_call"
        and message.get("name") == FINISH_TOOL_NAME
    )


def _rejected_finish_ids(messages: list[Any]) -> set[str]:
    """Попытки, которые тул или судья отклонили. Их отчёт в ленту не идёт."""
    finish_ids: set[str] = set()
    for message in messages:
        if not _is_finish_call(message):
            continue
        call_id = message.get("call_id")
        if isinstance(call_id, str) and call_id != "":
            finish_ids.add(call_id)
    rejected: set[str] = set()
    for message in messages:
        if not isinstance(message, dict):
            continue
        if message.get("type") != "function_call_output":
            continue
        call_id = message.get("call_id")
        failed = _output_failed(message.get("output"))
        if call_id not in finish_ids or not failed:
            continue
        rejected.add(call_id)
    return rejected


def _output_failed(output: Any) -> bool:
    if isinstance(output, dict):
        payload = output
    elif isinstance(output, str):
        try:
            payload = json.loads(output)
        except json.JSONDecodeError:
            return False
    else:
        return False
    if not isinstance(payload, dict):
        return False
    return payload.get("ok") is False


def _accepted_finish_text(
    message: dict, rejected: set[str], last_agent: str
) -> str:
    call_id = message.get("call_id")
    if isinstance(call_id, str) and call_id in rejected:
        return ""
    view = SubmitSolution.finish_view(message.get("arguments"))
    if view is None or view.report == "":
        return ""
    if last_agent.strip() == view.report:
        return ""
    return view.render()


def _tool_report(message: Any, seen: set[str]) -> dict | None:
    if not isinstance(message, dict):
        return None
    return report_from_record(message, seen)


def _records_by_parent(records: list[Any]) -> dict[str, list[dict]]:
    grouped: dict[str, list[dict]] = {}
    for record in records:
        if not isinstance(record, dict):
            continue
        parent = record.get("parent_call_id")
        if not isinstance(parent, str) or parent == "":
            continue
        grouped.setdefault(parent, []).append(record)
    for rows in grouped.values():
        rows.sort(key=_record_seq)
    return grouped


def _record_seq(record: dict) -> int:
    seq = record.get("seq")
    if isinstance(seq, int):
        return seq
    return 0


def _nested_tool_updates(
    parent_id: str,
    grouped: dict[str, list[dict]],
    seen: set[str],
    consumed: set[int],
    stack: set[str],
) -> list[dict]:
    """Тулы субагента и, рекурсивно, тулы вложенного субагента."""
    rows = grouped.get(parent_id)
    if not rows or parent_id in stack:
        return []
    stack.add(parent_id)
    updates: list[dict] = []
    for record in rows:
        mark = id(record)
        if mark in consumed:
            continue
        consumed.add(mark)
        report = report_from_sidecar(record, seen)
        if report is not None:
            updates.append(report)
        if record.get("kind") != "call":
            continue
        payload = record.get("payload")
        raw = payload.get("call_id") if isinstance(payload, dict) else None
        if isinstance(raw, str) and raw != "":
            updates.extend(
                _nested_tool_updates(raw, grouped, seen, consumed, stack)
            )
    stack.remove(parent_id)
    return updates


def _leftover_tool_updates(
    records: list[Any], consumed: set[int], seen: set[str]
) -> list[dict]:
    """Sidecar, чей родительский вызов в тред не попал."""
    pending = [
        record
        for record in records
        if isinstance(record, dict) and id(record) not in consumed
    ]
    pending.sort(key=_record_seq)
    updates: list[dict] = []
    for record in pending:
        report = report_from_sidecar(record, seen)
        if report is not None:
            updates.append(report)
    return updates


def _history_chunk(message: Any) -> dict | None:
    if not isinstance(message, dict) or _skipped(message):
        return None
    role = str(message.get("role") or "").strip().lower()
    kind = _ROLE_KIND.get(role)
    if kind is None:
        return None
    text = _role_text(role, message).strip()
    if text == "":
        return None
    return text_chunk(kind, text, new_message_id())


def _skipped(message: dict) -> bool:
    msg_type = str(message.get("type") or "message")
    if msg_type in _SKIP_TYPES:
        return True
    return str(message.get("role") or "").strip().lower() == "tool"


def _role_text(role: str, message: dict) -> str:
    if role == "user":
        real = extract_real_user_message_text(message)
        if real == "":
            return ""
        # display_content — уже человеко-текст; иначе только text/output_text,
        # без сырого JSON картинок из user_content_to_text.
        if message.get("display_content") is not None:
            return real
        return plain_text(message.get("content"))
    plain = plain_text(message.get("content"))
    return project_assistant_text(plain, strip_think=True)


def plain_text(content: Any) -> str:
    """Только текстовые части. Картинка и сырой JSON блока наружу не идут."""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(_text_items(content))
    if isinstance(content, dict) and _is_text_part(content):
        text = content.get("text")
        if isinstance(text, str):
            return text
    return ""


def _text_items(content: list) -> list[str]:
    parts: list[str] = []
    for item in content:
        if isinstance(item, str):
            parts.append(item)
            continue
        if isinstance(item, dict) and _is_text_part(item):
            text = item.get("text")
            if isinstance(text, str):
                parts.append(text)
    return parts


def _is_text_part(item: dict) -> bool:
    # Ядро пишет прозу каноном output_text (convertations._canonical_message_item).
    return item.get("type") in (None, "text", "output_text")
