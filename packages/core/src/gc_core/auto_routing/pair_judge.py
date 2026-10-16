"""Судья пары (политика `two_level`, `safe_level: auto`): карточка хода и разбор
оценки сложности 1–5. Чистые функции над State: ни ctx, ни бэкенда — вызов живёт
в `judge_runner.arun_pair_judge`. Импортируют модуль только `judge_runner` и тесты."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from gc_core.auto_routing.policy import PAIR_SCORE_MAX, PAIR_SCORE_MIN
from gc_core.auto_routing.turn_text import task_text
from gc_core.bundled_assets import bundled_root
from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.intent_log.record import ACTION_FINAL_ANSWER, ACTION_USER_ASK
from gc_core.intent_log.serialize import deserialize_list
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    is_dialog_summary_user_message_text,
    user_content_to_text,
)
from gc_core.utils.text_preview import head_tail_preview

#: Лимиты карточки — константы модуля, не конфиг: судья обязан оставаться дешёвым.
TASK_MAX_CHARS = 2_000
PREVIOUS_MAX_CHARS = 1_000

SOURCE_JUDGE = "judge"
SOURCE_FALLBACK = "fallback"


def default_pair_judge_prompt_path() -> str:
    return str(
        bundled_root()
        / "prompts"
        / "chat_completions_routing_pair_judge_prompt.md"
    )


def __getattr__(name: str):
    if name == "DEFAULT_PROMPT_PATH":
        return default_pair_judge_prompt_path()
    raise AttributeError(name)


def prompt_path(source: Any) -> str:
    """Путь инструкции судьи пары: из конфига (`paths.…`), иначе встроенный."""
    from gc_core.settings import settings_of

    return (
        settings_of(
            source
        ).paths.chat_completions_routing_pair_judge_prompt_path
        or default_pair_judge_prompt_path()
    )


_FENCED_OBJECT = re.compile(r"```(?:json)?\s*(\{.*?\})\s*```", re.DOTALL)


@dataclass(frozen=True)
class PairChoice:
    """Выбор пары хода: уровень хода (сильный уровень пары), оценка и фраза судьи,
    источник (`judge` | `fallback`) и причина сбоя."""

    safe_level: str
    score: Optional[int]
    reason: str
    source: str
    error: Optional[str]


def build_pair_card(state: Any, *, legacy: bool = False) -> str:
    """Карточка судьи пары (JSON): номер хода, признак сжатой истории, текущий
    запрос и — на продолжении — запрос и итог прошлого хода."""
    card: dict = {
        "turn": int(getattr(state, "user_turn_seq", 0) or 0) + 1,
        "earlier_history_compacted": _history_compacted(state),
        "request": task_text(state, limit=TASK_MAX_CHARS, legacy=legacy),
    }
    previous = _previous_turn(state, legacy=legacy)
    if previous:
        card["previous_turn"] = previous
    return json.dumps(card, ensure_ascii=False, indent=1)


def _previous_turn(state: Any, *, legacy: bool = False) -> dict:
    log = deserialize_list(getattr(state, "user_intent_log", None) or [])
    turns = [r.turn for r in log if isinstance(r.turn, int)]
    if not turns:
        return {}
    last_turn = max(turns)
    records = [r for r in log if r.turn == last_turn]
    asks = [
        str(r.content).strip()
        for r in records
        if r.action == ACTION_USER_ASK and isinstance(r.content, str)
    ]
    request = "\n".join(a for a in asks if a)
    block = {"request": _preview(request, legacy=legacy)}
    outcome = _final_answer(records, legacy=legacy) or _last_finish(
        state, records=records, legacy=legacy
    )
    if outcome:
        block["outcome"] = outcome
    elif not legacy:
        block["outcome_available"] = False
    return block


def _preview(text: str, *, legacy: bool) -> str:
    return (
        text[:PREVIOUS_MAX_CHARS]
        if legacy
        else head_tail_preview(text, PREVIOUS_MAX_CHARS)
    )


def _final_answer(records: list, *, legacy: bool = False) -> str:
    """Сохранённый итог завершённого хода: проза или разрешённый turn_finish."""
    for record in reversed(records):
        if record.action != ACTION_FINAL_ANSWER:
            continue
        content = record.content
        text = (
            content
            if isinstance(content, str)
            else json.dumps(content, ensure_ascii=False)
        )
        return _preview(text.strip(), legacy=legacy)
    return ""


def _last_finish(state: Any, *, records: list, legacy: bool = False) -> str:
    """Старый итог — только если trace подтверждает завершение нужного хода."""
    messages = _messages_before_turn(state)
    # При attach граница бывает после сохранённого незакрытого хода.
    # Его отчёт нельзя приписывать последнему закрытому ходу журнала.
    if not legacy and _has_open_ask(state, messages):
        return ""
    index = next(
        (
            i
            for i in range(len(messages) - 1, -1, -1)
            if _is_finish_call(messages[i])
        ),
        None,
    )
    if index is None:
        return ""
    gate = {} if legacy else _submitted_finish_gate(messages, index, records)
    if gate is None:
        return ""
    return _preview(_finish_text(messages[index], gate), legacy=legacy)


def _messages_before_turn(state: Any) -> list:
    messages = list(getattr(state, "messages", None) or [])
    start = getattr(state, "turn_messages_start", None)
    if start is not None and int(start) <= len(messages):
        messages = messages[: int(start)]
    return messages


def _has_open_ask(state: Any, messages: list) -> bool:
    open_asks = {
        str(r.content).strip()
        for r in deserialize_list(
            getattr(state, "current_user_intent", None) or []
        )
        if r.action == ACTION_USER_ASK
    }
    return any(
        isinstance(m, dict)
        and extract_real_user_message_text(m).strip() in open_asks
        for m in messages
    )


def _is_finish_call(message: Any) -> bool:
    return (
        isinstance(message, dict)
        and message.get("type") == "function_call"
        and message.get("name") == FINISH_TOOL_NAME
    )


def _submitted_finish_gate(messages: list, index: int, records: list) -> Any:
    """Решение гейта для turn_finish `messages[index]`, если trace подтверждает его
    приём в закрытом ходе журнала; `None` — подтверждения нет."""
    closed_asks = {
        str(r.content).strip() for r in records if r.action == ACTION_USER_ASK
    }
    if _preceding_ask(messages[:index]) not in closed_asks:
        return None
    call_id = messages[index].get("call_id")
    if not isinstance(call_id, str) or not call_id:
        return None
    output = _last_call_output(messages[index + 1 :], call_id)
    if (
        not isinstance(output, dict)
        or output.get("ok") is not True
        or output.get("status") != "submitted"
    ):
        return None
    gate = output.get("finish_gate") or {}
    if isinstance(gate, dict) and gate.get("action") == "reject":
        return None
    return gate


def _preceding_ask(messages: list) -> str:
    texts = (
        extract_real_user_message_text(m).strip()
        for m in reversed(messages)
        if isinstance(m, dict)
    )
    return next((text for text in texts if text), "")


def _last_call_output(messages: list, call_id: str) -> Any:
    outputs = [
        m.get("output")
        for m in messages
        if isinstance(m, dict)
        and m.get("type") == "function_call_output"
        and m.get("call_id") == call_id
    ]
    output = outputs[-1] if outputs else None
    if not isinstance(output, str):
        return output
    try:
        return json.loads(output)
    except ValueError:
        return None


def _finish_text(message: dict, gate: Any) -> str:
    """`тип: отчёт` из аргументов turn_finish; непринятый гейтом итог помечен."""
    try:
        args = json.loads(message.get("arguments") or "{}")
    except (TypeError, ValueError):
        return ""
    if not isinstance(args, dict):
        return ""
    kind = str(args.get("solution_type") or "").strip() + _fail_open_mark(gate)
    report = str(args.get("task_report") or "").strip()
    return f"{kind}: {report}" if kind and report else (kind or report)


def _fail_open_mark(gate: Any) -> str:
    runtime_outcome = (
        gate.get("runtime_outcome") if isinstance(gate, dict) else None
    )
    return (
        f" [{runtime_outcome}]"
        if runtime_outcome in ("fail_open_error", "fail_open_limit")
        else ""
    )


def _history_compacted(state: Any) -> bool:
    return any(
        isinstance(m, dict)
        and str(m.get("role") or "") == "user"
        and is_dialog_summary_user_message_text(
            user_content_to_text(m.get("content"))
        )
        for m in (getattr(state, "messages", None) or [])
    )


def parse_pair_score(text: str) -> tuple[Optional[int], str, Optional[str]]:
    """Строгий разбор ответа `{"score": 1..5, "reason": "…"}` — голый или в
    json-блоке. Возвращает `(оценка, фраза, None)` или `(None, "", причина сбоя)`:
    `bad_json`, `no_score`, `bad_type`, `out_of_range`."""
    raw = (text or "").strip()
    fenced = _FENCED_OBJECT.search(raw)
    if fenced is not None:
        raw = fenced.group(1)
    try:
        payload = json.loads(raw)
    except (TypeError, ValueError):
        return None, "", "bad_json"
    if not isinstance(payload, dict):
        return None, "", "bad_json"
    if "score" not in payload:
        return None, "", "no_score"
    score = _as_score(payload["score"])
    if score is None:
        return None, "", "bad_type"
    if not PAIR_SCORE_MIN <= score <= PAIR_SCORE_MAX:
        return None, "", "out_of_range"
    reason = payload.get("reason")
    return score, reason.strip() if isinstance(reason, str) else "", None


def _as_score(value: Any) -> Optional[int]:
    # bool — подкласс int: `true` иначе стал бы оценкой 1.
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if (
        isinstance(value, str)
        and len(value.strip()) == 1
        and value.strip().isdigit()
    ):
        return int(value.strip())
    return None
