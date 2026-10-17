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
#: Наименьшая справедливая доля старого сообщения пользователя: меньше —
#: обрезок теряет смысл, и вместо этого выпадают самые старые сообщения.
MESSAGE_SHARE_MIN_CHARS = 1_000
ACTIONS_TOTAL_CHARS = 40_000
HISTORY_RECORDS = 40  # записей истории, кроме сообщений пользователя
JUDGED_ACTION_CHARS = 16_000  # аргументы проверяемого действия

KIND_USER = "user"
#: Слова пользователя из канала, объявившего их внешними (вебхук сервера,
#: пир A2A): описание задачи и её запреты, но не разрешение (спека §3.7).
KIND_EXTERNAL = "external"
KIND_AGENT = "agent"
KIND_ACTION = "action"
#: Виды записей со словами пользователя: у них свой бюджет запроса, и
#: история действий их не вытесняет.
USER_WORD_KINDS = frozenset({KIND_USER, KIND_EXTERNAL})

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
    """Запись переписки для судьи: слова пользователя (доверенные) или
    запрос внешней системы, слова агента или его прежнее действие
    (недоверенные)."""

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


def words_within_budget(words: Sequence[str]) -> list[str]:
    """Слова человека, урезанные тем же правилом, что в запросе судьи
    (``_retained_user_words``): самое свежее — целиком, старые делят остаток
    бюджета справедливой долей. Для переноса через границу с лимитом
    размера (вход субагента под Temporal)."""
    entries = [ConversationEntry.user(str(text)) for text in words]
    kept = _retained_user_words(entries)
    return [kept[index][KIND_USER] for index in sorted(kept)]


def _cut_middle(text: str, limit: int) -> str:
    """Текст в пределах `limit` знаков вместе с пометкой: начало и конец
    сохраняются, вырезается середина — цель команды и запрет пользователя
    чаще стоят в конце."""
    if len(text) <= limit:
        return text
    kept = limit - len(_CUT_MARK)
    head = kept // 2
    return text[:head] + _CUT_MARK + text[len(text) - (kept - head) :]


def _bounded_arguments(arguments: Any, limit: int) -> dict[str, Any]:
    """Аргументы как есть, пока помещаются; иначе — начало и конец с
    вырезанной серединой и явный признак: судья не должен принять обрезок за
    целое. Цель команды и хвост записанного скрипта чаще стоят в конце."""
    dump = json.dumps(arguments, ensure_ascii=False, default=str)
    if len(dump) <= limit:
        return {"arguments": arguments}
    return {"arguments": _cut_middle(dump, limit), "arguments_truncated": True}


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
    text = _cut_middle(str(entry.text or ""), MESSAGE_CHARS)
    return {entry.kind: text}, len(text)


def _fair_cap(lengths: Sequence[int], budget: int) -> int:
    """Наибольший общий потолок ``c`` (не больше ``MESSAGE_CHARS``), при
    котором сумма ``min(длина, c)`` укладывается в ``budget``: короткие
    сообщения идут целиком, длинные делят остаток поровну."""
    left = len(lengths)
    for length in sorted(min(n, MESSAGE_CHARS) for n in lengths):
        if length * left > budget:
            return budget // left
        budget -= length
        left -= 1
    return MESSAGE_CHARS


def _retained_user_words(
    conversation: Sequence[ConversationEntry],
) -> dict[int, dict[str, Any]]:
    """Слова пользователя: самое свежее — целиком (в пределах
    ``MESSAGE_CHARS``), более старые делят остаток бюджета справедливой
    долей. Доля вышла бы меньше ``MESSAGE_SHARE_MIN_CHARS`` — выпадают самые
    старые."""
    spoken = [
        index
        for index, entry in enumerate(conversation)
        if entry.kind in USER_WORD_KINDS
    ]
    if not spoken:
        return {}
    *older, freshest = spoken
    texts = {index: str(conversation[index].text or "") for index in spoken}
    kept = {freshest: _cut_middle(texts[freshest], MESSAGE_CHARS)}
    budget = MESSAGES_TOTAL_CHARS - len(kept[freshest])
    lengths = [len(texts[index]) for index in older]
    # Чем больше старых выпало, тем крупнее доля: ищем наименьшее число
    # выпавших, при котором доля не меньше порога.
    low, high = 0, len(older)
    while low < high:
        middle = (low + high) // 2
        if _fair_cap(lengths[middle:], budget) < MESSAGE_SHARE_MIN_CHARS:
            low = middle + 1
        else:
            high = middle
    older = older[low:]
    cap = _fair_cap(lengths[low:], budget)
    kept.update({index: _cut_middle(texts[index], cap) for index in older})
    return {
        index: {conversation[index].kind: text} for index, text in kept.items()
    }


def _retained_history(
    conversation: Sequence[ConversationEntry],
) -> dict[int, dict[str, Any]]:
    """Слова агента и его действия — сплошной хвост с конца в пределах
    ``ACTIONS_TOTAL_CHARS`` и ``HISTORY_RECORDS``."""
    kept: dict[int, dict[str, Any]] = {}
    budget = ACTIONS_TOTAL_CHARS
    for index in range(len(conversation) - 1, -1, -1):
        entry = conversation[index]
        if entry.kind in USER_WORD_KINDS:
            continue
        if len(kept) >= HISTORY_RECORDS:
            break
        rendered, cost = _render_entry(entry)
        if cost > budget:
            break
        budget -= cost
        kept[index] = rendered
    return kept


def _retained_conversation(
    conversation: Sequence[ConversationEntry],
) -> list[dict[str, Any]]:
    """Записи в пределах потолков, в исходном порядке.

    Слова пользователя и остальное считаются раздельно: длинная история
    действий не вытесняет то, что человек просил. На месте каждого пропуска —
    счётчик выпавших записей. Конец переписки не выпадает: самое свежее
    слово пользователя и последняя запись истории помещаются всегда."""
    kept = {
        **_retained_user_words(conversation),
        **_retained_history(conversation),
    }
    result: list[dict[str, Any]] = []
    omitted = 0
    for index in range(len(conversation)):
        rendered = kept.get(index)
        if rendered is None:
            omitted += 1
            continue
        if omitted:
            result.append({"omitted_earlier_entries": omitted})
            omitted = 0
        result.append(rendered)
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
        **_bounded_arguments(request.arguments, JUDGED_ACTION_CHARS),
    }
    return payload


#: Правило о записях внешнего запроса — отдельная часть инструкции судьи.
EXTERNAL_PROMPT = "external"


def action_judge_prompt_kinds(external: bool) -> tuple[str, ...]:
    """Части инструкции, нужные запросу: правило о внешнем запросе — только
    когда такие записи в нём есть."""
    if external:
        return ("system", "user", EXTERNAL_PROMPT)
    return ("system", "user")


def has_external_words(payload: Mapping[str, Any]) -> bool:
    """Есть ли в запросе записи внешнего запроса (`KIND_EXTERNAL`)."""
    return any(
        isinstance(entry, Mapping) and KIND_EXTERNAL in entry
        for entry in payload.get("conversation") or ()
    )


def load_action_judge_prompt(
    kind: str, *, store: AgentStore = DEFAULT_AGENT_STORE
) -> str:
    if kind not in action_judge_prompt_kinds(True):
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
    пробой: под durable-исполнением ядру читать файлы нельзя.

    Правило о внешнем запросе дописывается к инструкции, только если такие
    записи есть: запрос CLI и TUI остаётся побайтно прежним."""
    external = has_external_words(payload)
    if prompts is None:
        prompts = {
            kind: load_action_judge_prompt(kind, store=store)
            for kind in action_judge_prompt_kinds(external)
        }
    system = prompts["system"]
    if external:
        system = f"{system}\n\n{prompts[EXTERNAL_PROMPT]}"
    user = prompts["user"].replace(
        "{judge_payload_json}",
        json.dumps(payload, ensure_ascii=False, indent=1, default=str),
    )
    return [
        {"role": "system", "content": system},
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
