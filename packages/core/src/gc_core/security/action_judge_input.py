"""Запрос судье действий из состояния прогона.

Собирает переписку по порядку и размечает доверие по ПРОИСХОЖДЕНИЮ записи:
слова человека — доверенные, слова и действия агента — нет. Вывод
инструментов не берётся никогда; единственное исключение — ответ человека на
вопрос агента, потому что это его слова, а не чей-то текст.

Порядок задаёт `state.messages`. Слова пользователя, которых там уже нет (ушли
в сводку при сжатии истории), добавляются из журнала намерений в начало:
запросы и границы человека сжатие переживают, действия агента — нет.

В канале, объявившем слова пользователя внешними (сервер, A2A), все они идут
видом `external`: описание задачи и запреты, но не разрешение. Признак — на
прогоне, а не на сообщении: журнал намерений источник слов не хранит.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import replace
from typing import Any

from gc_core.constants.tool_names import ASK_HUMAN_TOOL_NAME
from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
    user_content_to_text,
)
from gc_core.plan_store import plan_is_accepted
from gc_core.security.action_judge import (
    KIND_EXTERNAL,
    KIND_USER,
    OUTCOME_UNKNOWN,
    ConversationEntry,
    JudgeRequest,
)
from gc_core.security.permission_mode import (
    CAT_EDIT,
    CAT_EXEC,
    CAT_MCP,
    CAT_NETWORK,
    CAT_SKILL_EXEC,
    CAT_SUBAGENT,
    CAT_TURN,
    CAT_UNKNOWN,
)
from gc_core.tools.arguments_validation import (
    arguments_without_service_fields,
)

#: Категории вызовов, которые судья видит как прежние действия агента. Чтение,
#: поиск и внутренние вызовы (заметки, память) действиями не считаются.
_ACTION_CATEGORIES = frozenset(
    {
        CAT_EDIT,
        CAT_EXEC,
        CAT_NETWORK,
        CAT_MCP,
        CAT_SKILL_EXEC,
        CAT_SUBAGENT,
        CAT_UNKNOWN,
    }
)
_CALL_TYPES = ("function_call", "tool_call")
_OUTPUT_TYPES = ("function_call_output", "tool_call_output")
#: Заглушка пустого ответа человека (`AskHuman`): согласием не считается.
_EMPTY_HUMAN_RESPONSE = "(empty response)"
_ERROR_TYPE = re.compile(r"[a-z0-9_]{1,64}")


def _loads(raw: Any) -> Any:
    if not isinstance(raw, str):
        return raw
    try:
        return json.loads(raw)
    except (ValueError, TypeError):
        return raw


def _category(name: str, tools_data: Mapping[str, Any]) -> str:
    entry = tools_data.get(name)
    if not isinstance(entry, Mapping):
        return CAT_UNKNOWN
    return str(entry.get("category") or CAT_UNKNOWN)


def _outcome(output: Any) -> str:
    """Исход прежнего действия без единого слова его вывода."""
    if output is None:
        return OUTCOME_UNKNOWN
    data = _loads(output)
    if isinstance(data, Mapping) and data.get("ok") is False:
        error = data.get("error")
        kind = error.get("type") if isinstance(error, Mapping) else None
        # Тип ошибки — короткий идентификатор; всё прочее в выводе тула
        # может быть чужим текстом и в запрос не идёт.
        if isinstance(kind, str) and _ERROR_TYPE.fullmatch(kind):
            return f"error:{kind}"
        return "error"
    return "ok"


def _human_answer(output: Any) -> str:
    """Ответ человека на вопрос агента — его слова; пустой ответ и сбой
    вопроса согласием не считаются."""
    data = _loads(output)
    if not isinstance(data, Mapping) or data.get("ok") is not True:
        return ""
    answer = str(data.get("response") or "").strip()
    return "" if answer == _EMPTY_HUMAN_RESPONSE else answer


def _spoken_text(arguments: Any) -> str:
    """Что агент сказал человеку вызовом управления ходом (вопрос, ответ,
    отчёт, план): строковые аргументы вызова."""
    if isinstance(arguments, Mapping):
        parts = [v.strip() for v in arguments.values() if isinstance(v, str)]
        return "\n".join(part for part in parts if part)
    return arguments.strip() if isinstance(arguments, str) else ""


def _journal_user_asks(state: Any) -> list[str]:
    texts: list[str] = []
    for name in ("user_intent_log", "current_user_intent"):
        for record in getattr(state, name, None) or ():
            if getattr(record, "action", None) == ACTION_USER_ASK:
                text = str(getattr(record, "content", "") or "").strip()
                if text and text not in texts:
                    texts.append(text)
    return texts


def _cut_at_call(
    messages: Sequence[Any], call_id: str | None
) -> Sequence[Any]:
    """Всё, что было до проверяемого вызова. Соседи по пачке, стоящие после
    него, ещё не исполнены и в историю не входят."""
    if call_id:
        for index in range(len(messages) - 1, -1, -1):
            item = messages[index]
            if (
                isinstance(item, Mapping)
                and item.get("type") in _CALL_TYPES
                and item.get("call_id") == call_id
            ):
                return messages[:index]
    return messages


def _output_of(messages: Sequence[Any], call_index: int) -> Any:
    """Вывод ЭТОГО вызова: первый вывод с его идентификатором, стоящий после
    него. Идентификаторы выдаёт провайдер, и повторный не должен подменить
    исход прежнего действия или ответ человека на прежний вопрос."""
    call_id = messages[call_index].get("call_id")
    if not call_id:
        return None
    for item in messages[call_index + 1 :]:
        if not isinstance(item, Mapping):
            continue
        if item.get("call_id") != call_id:
            continue
        if item.get("type") in _OUTPUT_TYPES:
            return item.get("output")
        if item.get("type") in _CALL_TYPES:
            return None  # идентификатор занят следующим вызовом
    return None


def _conversation(
    messages: Sequence[Any], tools_data: Mapping[str, Any]
) -> list[ConversationEntry]:
    entries: list[ConversationEntry] = []
    for index, item in enumerate(messages):
        if not isinstance(item, Mapping):
            continue
        if item.get("type") in _CALL_TYPES:
            entries.extend(
                _call_entries(item, _output_of(messages, index), tools_data)
            )
        else:
            entries.extend(_message_entries(item))
    return entries


def _call_entries(
    item: Mapping, output: Any, tools_data: Mapping[str, Any]
) -> list[ConversationEntry]:
    """Вызов тула глазами судьи: слова агента и ответ человека из тулов хода,
    действие с исходом — из тулов, меняющих мир; прочее не показывается."""
    name = str(item.get("name") or "")
    entry = tools_data.get(name)
    raw = item.get("arguments")
    if isinstance(entry, Mapping):
        raw = arguments_without_service_fields(raw, dict(entry))
    arguments = _loads(raw)
    category = _category(name, tools_data)
    if category in _ACTION_CATEGORIES:
        return [ConversationEntry.action(name, arguments, _outcome(output))]
    if category != CAT_TURN:
        return []
    entries: list[ConversationEntry] = []
    said = _spoken_text(arguments)
    if said:
        entries.append(ConversationEntry.agent(said))
    if name == ASK_HUMAN_TOOL_NAME and (answer := _human_answer(output)):
        entries.append(ConversationEntry.user(answer))
    return entries


def _message_entries(item: Mapping) -> list[ConversationEntry]:
    """Сообщение переписки: настоящие слова человека и текст агента."""
    role = str(item.get("role") or "").strip().lower()
    if role == "user":
        text = extract_real_user_message_text(item)
        return [ConversationEntry.user(text)] if text else []
    if role == "assistant":
        text = user_content_to_text(item.get("content")).strip()
        return [ConversationEntry.agent(text)] if text else []
    return []


def user_words(state: Any, tools_data: Mapping[str, Any]) -> list[str]:
    """Все слова пользователя, известные этому прогону, по порядку: ушедшие в
    сводку (из журнала намерений), сообщения переписки и ответы на вопросы
    агента. Этим же списком раннер субагента снабжает судью ребёнка."""
    conversation = _conversation(
        list(getattr(state, "messages", None) or ()), tools_data
    )
    spoken = [entry.text for entry in conversation if entry.kind == "user"]
    earlier = [
        text for text in _journal_user_asks(state) if text not in spoken
    ]
    return [*earlier, *spoken]


def _root_user_messages(ctx: Any) -> list[str]:
    """Слова пользователя корневой сессии для судьи субагента. Поставщика
    ставит раннер субагента; его может подменить источник поживее."""
    provider = getattr(ctx, "root_user_messages", None)
    if not callable(provider):
        return []
    return [
        str(text).strip() for text in provider() or () if str(text).strip()
    ]


def _take_delegated_task(
    journal: Sequence[str], conversation: list[ConversationEntry]
) -> str:
    """Задание родителя — первая запись журнала субагента и его стартовое
    сообщение. Это текст агента: из слов пользователя оно исключается."""
    seed = journal[0] if journal else ""
    for index, entry in enumerate(conversation):
        if entry.kind == "user":
            if not seed or entry.text == seed:
                seed = entry.text
                del conversation[index]
            break
    return seed


def _as_external(entry: ConversationEntry) -> ConversationEntry:
    """Слова пользователя внешнего канала — запрос внешней системы; слова
    агента и его действия — как были."""
    if entry.kind != KIND_USER:
        return entry
    return replace(entry, kind=KIND_EXTERNAL)


def build_judge_request(
    state: Any,
    ctx: Any,
    *,
    tool: str,
    category: str,
    arguments: Any,
    call_id: str | None,
    tools_data: Mapping[str, Any],
    flags: Iterable[str] = (),
    runtime: Mapping[str, Any] | None = None,
    external_user_words: bool = False,
) -> JudgeRequest:
    """Запрос судье по проверяемому вызову.

    ``external_user_words`` — канал объявил слова пользователя внешними
    (``RuntimeEnv.external_user_words``): тогда все они — сообщения,
    ответы на вопросы агента, слова журнала и слова корня у субагента —
    идут судье видом ``external``.
    """
    messages = _cut_at_call(
        list(getattr(state, "messages", None) or ()), call_id
    )
    conversation = _conversation(messages, tools_data)
    journal = _journal_user_asks(state)
    delegated_task = ""
    earlier: list[str] = []

    if getattr(state, "parent_state_id", None) is not None:
        delegated_task = _take_delegated_task(journal, conversation)
        journal = journal[1:]
        earlier.extend(_root_user_messages(ctx))

    spoken = {entry.text for entry in conversation if entry.kind == "user"}
    earlier.extend(text for text in journal if text not in spoken)
    conversation[:0] = [ConversationEntry.user(text) for text in earlier]
    if external_user_words:
        conversation = [_as_external(entry) for entry in conversation]

    plan = getattr(getattr(state, "shared", None), "plan", None)
    return JudgeRequest(
        conversation=tuple(conversation),
        tool=tool,
        category=category,
        arguments=arguments,
        flags=tuple(flags),
        approved_plan=str(plan.get("text") or "")
        if plan_is_accepted(plan)
        else "",
        delegated_task=delegated_task,
        runtime=dict(runtime or {}),
    )
