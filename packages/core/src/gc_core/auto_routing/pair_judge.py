"""Судья пары (политика `two_level`, `safe_level: auto`): карточка хода и разбор
оценки сложности 1–5. Чистые функции над State: ни ctx, ни бэкенда — вызов живёт
в `judge_runner.arun_pair_judge`. Импортируют модуль только `judge_runner` и тесты."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from typing import Any, Optional

from gc_core.agent_store import (
    DEFAULT_AGENT_STORE, AgentResource, AgentStore, read_required_text,
)
from gc_core.auto_routing.policy import (
    PAIR_SCORE_MAX, PAIR_SCORE_MIN, SAFE_LEVEL_AUTO, RoutingParams,
)
from gc_core.auto_routing.turn_text import task_text
from gc_core.bundled_assets import bundled_root
from gc_core.constants.tool_names import FINISH_TOOL_NAME
from gc_core.intent_log.record import ACTION_FINAL_ANSWER, ACTION_USER_ASK
from gc_core.intent_log.serialize import deserialize_list
from gc_core.message_policy.user_message_policy import (
    is_dialog_summary_user_message_text, user_content_to_text,
)

#: Лимиты карточки — константы модуля, не конфиг: судья обязан оставаться дешёвым.
TASK_MAX_CHARS = 2_000
PREVIOUS_MAX_CHARS = 1_000

SOURCE_JUDGE = "judge"
SOURCE_FALLBACK = "fallback"

def default_pair_judge_prompt_path() -> str:
    return str(bundled_root() / "prompts" / "chat_completions_routing_pair_judge_prompt.md")


def __getattr__(name: str):
    if name == "DEFAULT_PROMPT_PATH":
        return default_pair_judge_prompt_path()
    raise AttributeError(name)


PROMPT_PATH_KEY = "chat_completions_routing_pair_judge_prompt_path"


def prompt_path(source: Any) -> str:
    """Путь инструкции судьи пары: из конфига (`paths.…`), иначе встроенный."""
    from gc_core.settings import settings_of

    return settings_of(source).paths.chat_completions_routing_pair_judge_prompt_path or default_pair_judge_prompt_path()


def require_prompt(
    source: Any,
    params: RoutingParams,
    *,
    store: AgentStore = DEFAULT_AGENT_STORE,
    force_legacy: bool = False,
) -> None:
    """Проверить ресурс судьи пары до старта локального контекста, если auto доступен."""
    from gc_core.auto_routing.levels import auto_available

    if params.safe_level != SAFE_LEVEL_AUTO or not auto_available(source, force_legacy=force_legacy):
        return
    path = prompt_path(source)
    missing = (f"инструкция судьи пары не найдена: {path} "
               f"(paths.{PROMPT_PATH_KEY}, нужна при auto_routing_safe_level: auto)")
    try:
        text = read_required_text(
            store, AgentResource("prompt", "routing_pair_judge", path), missing=missing,
        )
    except FileNotFoundError as exc:
        raise ValueError(str(exc)) from None
    if not text.strip():
        raise ValueError(f"инструкция судьи пары пуста: {path} (paths.{PROMPT_PATH_KEY})")

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


def build_pair_card(state: Any) -> str:
    """Карточка судьи пары (JSON): номер хода, признак сжатой истории, текущий
    запрос и — на продолжении — запрос и итог прошлого хода."""
    card: dict = {
        "turn": int(getattr(state, "user_turn_seq", 0) or 0) + 1,
        "earlier_history_compacted": _history_compacted(state),
        "request": task_text(state, limit=TASK_MAX_CHARS),
    }
    previous = _previous_turn(state)
    if previous:
        card["previous_turn"] = previous
    return json.dumps(card, ensure_ascii=False, indent=1)


def _previous_turn(state: Any) -> dict:
    log = deserialize_list(getattr(state, "user_intent_log", None) or [])
    turns = [r.turn for r in log if isinstance(r.turn, int)]
    if not turns:
        return {}
    last_turn = max(turns)
    records = [r for r in log if r.turn == last_turn]
    asks = [str(r.content).strip() for r in records
            if r.action == ACTION_USER_ASK and isinstance(r.content, str)]
    block = {"request": "\n".join(a for a in asks if a)[:PREVIOUS_MAX_CHARS]}
    outcome = _final_answer(records) or _last_finish(state)
    if outcome:
        block["outcome"] = outcome
    return block


def _final_answer(records: list) -> str:
    """Итог хода, закончившегося ответом текстом (запись журнала есть только у него)."""
    for record in reversed(records):
        if record.action != ACTION_FINAL_ANSWER:
            continue
        content = record.content
        text = content if isinstance(content, str) else json.dumps(content, ensure_ascii=False)
        return text.strip()[:PREVIOUS_MAX_CHARS]
    return ""


def _last_finish(state: Any) -> str:
    """Итог хода, закрытого `turn_finish`: последний такой вызов до начала текущего
    хода — тип результата и отчёт. После сжатия истории его может не быть."""
    messages = list(getattr(state, "messages", None) or [])
    start = getattr(state, "turn_messages_start", None)
    if start is not None and int(start) <= len(messages):
        messages = messages[:int(start)]
    for message in reversed(messages):
        if not (isinstance(message, dict) and message.get("type") == "function_call"
                and message.get("name") == FINISH_TOOL_NAME):
            continue
        try:
            args = json.loads(message.get("arguments") or "{}")
        except (TypeError, ValueError):
            return ""
        if not isinstance(args, dict):
            return ""
        kind = str(args.get("solution_type") or "").strip()
        report = str(args.get("task_report") or "").strip()
        text = f"{kind}: {report}" if kind and report else (kind or report)
        return text[:PREVIOUS_MAX_CHARS]
    return ""


def _history_compacted(state: Any) -> bool:
    return any(
        isinstance(m, dict) and str(m.get("role") or "") == "user"
        and is_dialog_summary_user_message_text(user_content_to_text(m.get("content")))
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
    if isinstance(value, str) and len(value.strip()) == 1 and value.strip().isdigit():
        return int(value.strip())
    return None
