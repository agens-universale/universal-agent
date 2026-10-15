"""Судья действий режима auto: что уходит модели и как читается её ответ.

Слой политики. Здесь нет ни вызова модели, ни State: на вход — уже собранный
`JudgeRequest`, на выход — сообщения для вспомогательной модели и разобранный
вердикт. Кто собирает запрос из переписки и кто зовёт модель — дело
вызывающих (стадии режима).

Судья отвечает на три вопроса, а решение выводит программа (`action_blocked`):
так модель не может назвать действие рискованным и неразрешённым и тут же его
разрешить, а факты запроса (строгая пометка, обрезка) весят больше её ответа.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from gc_core.agent_store import DEFAULT_AGENT_STORE, AgentStore
from gc_core.bundled_assets import bundled_root
from gc_core.message_format.dialog_history import truncate_text
from gc_core.plan_store import PLAN_MAX_CHARS
from gc_core.security.command_moderation import (
    JudgeUnavailable,
    _extract_json_candidate,
    _load_prompt_file,
    _strip_judge_response_text,
)

# Потолки запроса, в знаках — как остальные лимиты конфига. Порядок величин
# взят у эталонного проверяющего (синхронный профиль, из расчёта 4 знака на токен);
# они ограничивают крайний случай, обычный запрос много меньше.
MESSAGE_CHARS = 20_000  # одно сообщение пользователя или ответ агента
ACTION_RECORD_CHARS = 4_000  # аргументы одного прежнего действия
MESSAGES_TOTAL_CHARS = 80_000
ACTIONS_TOTAL_CHARS = 40_000
HISTORY_RECORDS = 40  # записей истории, кроме сообщений пользователя
JUDGED_ACTION_CHARS = 16_000  # аргументы проверяемого действия

KIND_USER = "user"
KIND_AGENT = "agent"
KIND_ACTION = "action"

OUTCOME_UNKNOWN = "unknown"

FLAG_OUTSIDE_PROJECT = "outside_project"
#: Путь защищён настройками: файл инструкций, конфиг и хуки git, файл из
#: чёрного списка чтения. Разрешение — только по прямой просьбе про этот путь.
FLAG_PROTECTED_PATH = "protected_path"
FLAG_RESTRICTED_ADDRESS = "restricted_address"
#: Пометки, при которых действие рискованно по факту, что бы ни ответила модель:
#: ей остаётся один вопрос — разрешил ли это пользователь.
STRICT_FLAGS = frozenset({FLAG_PROTECTED_PATH, FLAG_RESTRICTED_ADDRESS})

_CUT_MARK = " ... [TEXT CUTTED] ... "


class ActionJudgeMalformed(ValueError):
    """Ответ модели — не вердикт. Это сбой проверки: о действии он ничего не
    говорит и не считается ни разрешением, ни отказом."""


@dataclass(frozen=True)
class ConversationEntry:
    """Запись переписки для судьи: слова пользователя (доверенные), слова
    агента или его прежнее действие (недоверенные)."""

    kind: str
    text: str = ""
    tool: str = ""
    arguments: Any = None
    outcome: str = OUTCOME_UNKNOWN

    @classmethod
    def user(cls, text: str) -> ConversationEntry:
        return cls(KIND_USER, text=text)

    @classmethod
    def agent(cls, text: str) -> ConversationEntry:
        return cls(KIND_AGENT, text=text)

    @classmethod
    def action(
        cls, tool: str, arguments: Any, outcome: str = OUTCOME_UNKNOWN
    ) -> ConversationEntry:
        return cls(
            KIND_ACTION, tool=tool, arguments=arguments, outcome=outcome
        )


@dataclass(frozen=True)
class JudgeRequest:
    """Всё, что судья узнаёт об одном действии."""

    conversation: Sequence[ConversationEntry]
    tool: str
    category: str
    arguments: Any
    flags: Sequence[str] = ()
    approved_plan: str = ""
    delegated_task: str = ""
    runtime: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ActionVerdict:
    crosses_boundary: bool
    risky: bool
    authorized: bool
    reason: str = ""


def action_blocked(verdict: ActionVerdict, payload: Mapping[str, Any]) -> bool:
    """Решение по ответам судьи и фактам запроса — его принимает программа.

    Два факта сильнее ответа модели: строгая пометка делает действие
    рискованным, а обрезанные аргументы рискованного действия не дают
    проверить, покрывает ли разрешение невидимую часть, — такое разрешение не
    засчитывается.
    """
    if verdict.crosses_boundary:
        return True
    risky = verdict.risky or bool(
        STRICT_FLAGS & set(payload.get("flags") or ())
    )
    if not risky:
        return False
    truncated = bool((payload.get("action") or {}).get("arguments_truncated"))
    return truncated or not verdict.authorized


def _cut_middle(text: str, limit: int) -> str:
    """Начало и конец в пределах `limit`: цель команды чаще стоит в конце."""
    head = limit // 2
    return text[:head] + _CUT_MARK + text[len(text) - (limit - head) :]


def _bounded_arguments(
    arguments: Any, limit: int, *, keep_tail: bool = False
) -> dict[str, Any]:
    """Аргументы как есть, пока помещаются; иначе — обрезанный текст и явный
    признак: судья не должен принять обрезок за целое. У проверяемого
    действия (`keep_tail`) вырезается середина."""
    dump = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(dump) <= limit:
        return {"arguments": arguments}
    cut = _cut_middle(dump, limit) if keep_tail else truncate_text(dump, limit)
    return {"arguments": cut, "arguments_truncated": True}


def _render_entry(entry: ConversationEntry) -> tuple[dict[str, Any], int]:
    """Запись в виде для запроса и её цена в знаках."""
    if entry.kind == KIND_ACTION:
        body = {
            "tool": entry.tool,
            **_bounded_arguments(entry.arguments, ACTION_RECORD_CHARS),
            "outcome": entry.outcome or OUTCOME_UNKNOWN,
        }
        return {KIND_ACTION: body}, len(
            json.dumps(body, ensure_ascii=False, default=str)
        )
    text = truncate_text(str(entry.text or ""), MESSAGE_CHARS)
    return {entry.kind: text}, len(text)


def _retained_conversation(
    conversation: Sequence[ConversationEntry],
) -> list[dict[str, Any]]:
    """Свежие записи в пределах потолков, в исходном порядке.

    Отбор идёт с конца. Сообщения пользователя и остальное считаются
    раздельно: длинная история действий не вытесняет то, что человек просил.
    Выпавшее из начала заменяется счётчиком.
    """
    kept: list[tuple[int, dict[str, Any]]] = []
    budgets = {True: MESSAGES_TOTAL_CHARS, False: ACTIONS_TOTAL_CHARS}
    records_left = HISTORY_RECORDS
    for index in range(len(conversation) - 1, -1, -1):
        entry = conversation[index]
        is_user = entry.kind == KIND_USER
        if not is_user and records_left <= 0:
            continue
        rendered, cost = _render_entry(entry)
        if cost > budgets[is_user]:
            # бюджет этого вида исчерпан: более старые записи вида не берём
            budgets[is_user] = 0
            continue
        budgets[is_user] -= cost
        if not is_user:
            records_left -= 1
        kept.append((index, rendered))
    kept.reverse()
    omitted = len(conversation) - len(kept)
    result = [rendered for _, rendered in kept]
    if omitted:
        # Счётчик встаёт перед первой сохранённой записью истории: сообщения
        # пользователя, оставшиеся раньше неё, стоят на своих местах.
        first_history = next(
            (
                pos
                for pos, (index, _) in enumerate(kept)
                if conversation[index].kind != KIND_USER
            ),
            len(result),
        )
        result.insert(first_history, {"omitted_earlier_entries": omitted})
    return result


def build_action_judge_payload(request: JudgeRequest) -> dict[str, Any]:
    """Запрос судье. Порядок ключей постоянный, проверяемое действие — в
    конце: неизменное начало запроса удобно кэшу провайдера."""
    payload: dict[str, Any] = {"runtime": dict(request.runtime)}
    if request.approved_plan:
        payload["approved_plan"] = truncate_text(
            request.approved_plan, PLAN_MAX_CHARS
        )
    if request.delegated_task:
        payload["delegated_task"] = truncate_text(
            request.delegated_task, MESSAGE_CHARS
        )
    payload["conversation"] = _retained_conversation(request.conversation)
    payload["flags"] = list(request.flags)
    payload["action"] = {
        "tool": request.tool,
        "category": request.category,
        **_bounded_arguments(
            request.arguments, JUDGED_ACTION_CHARS, keep_tail=True
        ),
    }
    return payload


def load_action_judge_prompt(
    kind: str, *, store: AgentStore = DEFAULT_AGENT_STORE
) -> str:
    if kind not in ("system", "user"):
        raise ValueError("Unknown action judge prompt kind.")
    name = f"action_judge_{kind}"
    path = bundled_root() / "prompts" / "security" / f"{name}.md"
    return _load_prompt_file(path, store=store, name=name)


def build_action_judge_messages(
    payload: Mapping[str, Any],
    *,
    store: AgentStore = DEFAULT_AGENT_STORE,
    prompts: Mapping[str, str] | None = None,
) -> list[dict[str, str]]:
    """Сообщения для модели. ``prompts`` — тексты инструкции, уже добытые
    пробой: под durable-исполнением ядру читать файлы нельзя."""
    if prompts is None:
        prompts = {
            kind: load_action_judge_prompt(kind, store=store)
            for kind in ("system", "user")
        }
    user = prompts["user"].replace(
        "{judge_payload_json}",
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
    )
    return [
        {"role": "system", "content": prompts["system"]},
        {"role": "user", "content": user},
    ]


_VERDICT_FIELDS = ("crosses_boundary", "risky", "authorized")


def parse_action_verdict(
    raw_text: str, *, tags: Sequence[str] = ()
) -> ActionVerdict:
    """Ответ модели → вердикт. Fail-closed: всё, кроме объекта с тремя
    булевыми ответами, — `ActionJudgeMalformed`."""
    try:
        text = _strip_judge_response_text(raw_text or "", tags=tags)
        data = json.loads(_extract_json_candidate(text)) if text else None
    except (json.JSONDecodeError, JudgeUnavailable) as exc:
        raise ActionJudgeMalformed("judge reply is not JSON") from exc
    if not isinstance(data, dict):
        raise ActionJudgeMalformed("judge reply is not a JSON object")
    answers = [data.get(name) for name in _VERDICT_FIELDS]
    if not all(isinstance(answer, bool) for answer in answers):
        raise ActionJudgeMalformed(
            "judge reply lacks boolean crosses_boundary / risky / authorized"
        )
    return ActionVerdict(
        *answers, reason=str(data.get("reason") or "").strip()
    )
