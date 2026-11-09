"""Дельты хода и история треда → session/update."""

from __future__ import annotations

import json
import uuid
from dataclasses import dataclass, field
from typing import Any

from gc_core.constants.tool_names import (
    FINISH_TOOL_NAME,
    PROPOSE_PLAN_TOOL_NAME,
)
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.message_projection import project_assistant_text
from gc_core.tools.turn.propose_plan import ProposePlan
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_core.tools.turn.turn_close import (
    TurnClose,
    closing_replies,
    finish_close,
    plan_close,
    without_repeated_body,
)

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
# Вызовы, закрывающие ход текстом для человека: отклонённая попытка в ленту
# не идёт.
_CLOSING_CALLS = frozenset({FINISH_TOOL_NAME, PROPOSE_PLAN_TOOL_NAME})


def new_message_id() -> str:
    return str(uuid.uuid4())


def text_chunk(session_update: str, text: str, message_id: str) -> dict:
    return {
        "sessionUpdate": session_update,
        "content": {"type": "text", "text": text},
        "messageId": message_id,
    }


class ChunkAssembler:
    """Один messageId на непрерывную серию чанков одного вида.

    ``prose_sent`` — ход клиента уже показал прозу до этого прогона
    (продолжение после одобрения плана): первая реплика — абзацем.
    """

    def __init__(self, *, prose_sent: bool = False) -> None:
        self._kind: str | None = None
        self._message_id: str | None = None
        self._answer = ""
        self._last = ""
        self._reply_shown = False
        self._prose_sent = prose_sent

    @property
    def prose_sent(self) -> bool:
        """Ход клиента уже показал прозу: следующая реплика — абзацем."""
        return self._prose_sent or self._answer != ""

    @property
    def reply_shown(self) -> bool:
        """Были ``answer``-дельты после последнего ``reset``."""
        return self._reply_shown

    @property
    def last_answer_text(self) -> str:
        """Текст ответа последнего запроса модели с answer-дельтами
        (после ``reset`` — до первой новой дельты тоже)."""
        return self._last

    def feed(self, kind: str, text: str) -> list[dict]:
        if kind == "reset":
            self._kind = None
            self._message_id = None
            self._reply_shown = False
            return []
        if kind not in ("answer", "think"):
            return []
        if not isinstance(text, str) or text == "":
            return []
        self._continue(kind)
        if kind == "answer":
            text = self._take_answer(text)
        update = _ANSWER if kind == "answer" else _THINK
        return [text_chunk(update, text, self._message_id or "")]

    def flush_answer(self, last_answer: Any) -> list[dict]:
        """Дослать отчёт, которого не было в последней answer-реплике."""
        if isinstance(last_answer, str) and last_answer == self._answer:
            return []
        extra = unseen_answer(self._last, last_answer)
        if extra == "":
            return []
        continued = (
            self._kind == "answer"
            and self._message_id is not None
            and isinstance(last_answer, str)
            and self._last != ""
            and last_answer.startswith(self._last)
        )
        if continued:
            self._last += extra
        else:
            self._kind = "answer"
            self._message_id = new_message_id()
            self._last = extra
            extra = _separated(extra, after_answer=self.prose_sent)
        self._answer += extra
        return [text_chunk(_ANSWER, extra, self._message_id or "")]

    def append_answer(self, text: str) -> list[dict]:
        """Текст конца хода отдельным сообщением: свой ``messageId``.

        Если прогон уже отдал ответ, текст идёт с ведущей пустой строкой:
        клиент склеивает подряд идущие сообщения агента.
        """
        if text == "":
            return []
        text = _separated(text, after_answer=self.prose_sent)
        self._answer += text
        return [text_chunk(_ANSWER, text, new_message_id())]

    def _take_answer(self, text: str) -> str:
        """Учесть answer-дельту. Первый текст нового ответа модели (после
        ``reset``) после уже отданной прозы — с ведущей пустой строкой:
        клиент склеивает прозу хода в один блок. Мысль внутри того же
        ответа его текст не разрывает.
        """
        if self._reply_shown:
            self._last += text
        else:
            self._last = text
            text = _separated(text, after_answer=self.prose_sent)
        self._answer += text
        self._reply_shown = True
        return text

    def _continue(self, kind: str) -> None:
        if kind == self._kind and self._message_id is not None:
            return
        self._kind = kind
        self._message_id = new_message_id()


def _separated(text: str, *, after_answer: bool) -> str:
    if after_answer and not text.startswith("\n"):
        return "\n\n" + text
    return text


def unseen_answer(sent: str, final: Any) -> str:
    """Хвост финала относительно последней answer-реплики.

    ``sent`` — текст последней реплики, не вся проза хода. Совпадение
    и продолжение этой реплики дают только недостающий хвост. Подстрока
    более ранней прозы финал не отменяет.
    """
    if not isinstance(final, str) or final == "":
        return ""
    if final.startswith(sent):
        return final[len(sent) :]
    return final


def history_updates(
    messages: list[Any],
    *,
    subagent_records: list[Any] | None = None,
    root: str | None = None,
) -> list[dict]:
    """Проигрыш треда: у каждого сообщения свой messageId.

    function_call встаёт карточкой между прозой. Результат тула — смена
    статуса той же карточки, не текст ответа. Принятый turn_finish
    становится отчётом или записью после ответа модели (правило —
    ``gc_core.tools.turn.turn_close``); отклонённая попытка и вердикт судьи
    не показываются. Принятый план (``turn_propose_plan``) — текстом
    целиком, карточкой не показывается.
    Служебные user-сообщения ядра (сводка, чек-лист, technical) в ленту
    не идут — та же ручка, что у заголовка сессии.

    Вызовы из sidecar субагента встают сразу после карточки этого субагента,
    до его результата: для клиента это такие же действия, как тулы родителя.
    Шаг, чей родительский вызов в тред не попал, не показывается: после
    компакции карточка субагента уже в сводке, и его шаги исчезают вместе с ней.
    ``root`` — cwd сессии: пути в подписях вызовов от него, как в живом потоке.
    """
    records = subagent_records or []
    replay = _Replay(
        seen=set(),
        grouped=_records_by_parent(records),
        rejected=_rejected_call_ids(messages),
        replies=closing_replies(messages),
        root=root,
    )
    for message in messages:
        _append_history_message(replay, message)
    return replay.rendered


@dataclass
class _Replay:
    seen: set[str]
    grouped: dict[str, list[dict]]
    rejected: set[str]
    replies: dict[str, str]
    root: str | None = None
    consumed: set[int] = field(default_factory=set)
    stack: set[str] = field(default_factory=set)
    rendered: list[dict] = field(default_factory=list)
    answered: bool = False
    last_prose: str = ""


def _append_history_message(replay: _Replay, message: Any) -> None:
    tool = _tool_report(message, replay.seen, replay.root)
    if tool is not None:
        _append_tool(replay, tool)
        return
    if _is_call(message, FINISH_TOOL_NAME):
        _append_finish(replay, message)
        return
    if _is_call(message, PROPOSE_PLAN_TOOL_NAME):
        _append_plan(replay, message)
        return
    _append_prose(replay, message)


def _append_tool(replay: _Replay, tool: dict) -> None:
    replay.rendered.append(tool)
    if tool.get("sessionUpdate") != "tool_call":
        return
    call_id = tool.get("toolCallId")
    if not isinstance(call_id, str) or call_id == "":
        return
    replay.rendered.extend(_nested_tool_updates(call_id, replay))


def _append_finish(replay: _Replay, message: dict) -> None:
    """Принятый turn_finish: ответ модели уже показан прозой выше, поэтому
    дописывается только отчёт или запись."""
    call_id = message.get("call_id")
    if call_id in replay.rejected:
        return
    view = SubmitSolution.finish_view(message.get("arguments"))
    if view is None:
        return
    close = finish_close(view, replay.replies.get(call_id, ""))
    _append_close(replay, close)


def _append_plan(replay: _Replay, message: dict) -> None:
    """Принятый план — текстом целиком: карточка тула скрыта (её потолок
    5000 знаков меньше плана)."""
    call_id = message.get("call_id")
    if call_id in replay.rejected:
        return
    plan = ProposePlan.plan_from_arguments(message.get("arguments"))
    if plan is None:
        return
    close = plan_close(plan, replay.replies.get(call_id, ""))
    _append_close(replay, close)


def _append_close(replay: _Replay, close: TurnClose) -> None:
    close = without_repeated_body(close, replay.last_prose)
    text = close.render(markdown=True, after_shown_reply=True)
    if text == "":
        return
    text = _separated(text, after_answer=replay.answered)
    replay.rendered.append(text_chunk(_ANSWER, text, new_message_id()))
    replay.answered = True


def _append_prose(replay: _Replay, message: Any) -> None:
    chunk = _history_chunk(message)
    if chunk is None:
        return
    is_user = chunk["sessionUpdate"] == _USER
    text = chunk["content"]["text"]
    if not is_user:
        # Как в живом ходе: реплика после прозы того же хода — абзацем.
        chunk["content"]["text"] = _separated(
            text, after_answer=replay.answered
        )
    replay.rendered.append(chunk)
    replay.answered = not is_user
    replay.last_prose = "" if is_user else text


def _is_call(message: Any, name: str) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "function_call"
        and message.get("name") == name
    )


def _rejected_call_ids(messages: list[Any]) -> set[str]:
    """Финалы и планы, которые тул или судья отклонили: в ленту не идут."""
    closing_ids = {
        message.get("call_id")
        for message in messages
        if any(_is_call(message, name) for name in _CLOSING_CALLS)
        and isinstance(message.get("call_id"), str)
        and message["call_id"] != ""
    }
    return {
        message["call_id"]
        for message in messages
        if isinstance(message, dict)
        and message.get("type") == "function_call_output"
        and message.get("call_id") in closing_ids
        and _output_failed(message.get("output"))
    }


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


def _tool_report(
    message: Any, seen: set[str], root: str | None
) -> dict | None:
    if not isinstance(message, dict):
        return None
    return report_from_record(message, seen, root=root)


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


def _nested_tool_updates(parent_id: str, replay: _Replay) -> list[dict]:
    """Тулы субагента и, рекурсивно, тулы вложенного субагента."""
    rows = replay.grouped.get(parent_id)
    if not rows or parent_id in replay.stack:
        return []
    replay.stack.add(parent_id)
    updates: list[dict] = []
    for record in rows:
        mark = id(record)
        if mark in replay.consumed:
            continue
        replay.consumed.add(mark)
        report = report_from_sidecar(record, replay.seen, root=replay.root)
        if report is not None:
            updates.append(report)
        if record.get("kind") != "call":
            continue
        payload = record.get("payload")
        raw = payload.get("call_id") if isinstance(payload, dict) else None
        if isinstance(raw, str) and raw != "":
            updates.extend(_nested_tool_updates(raw, replay))
    replay.stack.remove(parent_id)
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
