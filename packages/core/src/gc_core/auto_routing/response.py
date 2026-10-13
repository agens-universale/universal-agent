"""Чтение ответа модели для автовыбора: состав батча для решения о переигрыше
и выброшенный переигрышем ответ для события лога. Чистые функции над сообщениями
ответа и
реестром тулов — ни State, ни ctx, ни логирования.

Ответ читается фильтром по `function_call`, а не строгой группировкой
(`processing_responses.group_messages` кидает `ValueError` на незнакомом типе
сообщения): читатели зовутся и на сыром ответе модели, до исполнения батча.

Импортируют модуль только контроллер (`processing_responses`) и
`auto_routing.reroute` — не `policy`, `levels`, `state_ops`: модуль тянет
пакет `gc_core.tools`, который сам импортирует `auto_routing.levels`/`policy`,
и обратный импорт замкнул бы цикл."""
from __future__ import annotations

from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

from gc_core.constants.tool_names import IMPORTANT_TOOL_NAMES
from gc_core.message_projection import project_assistant_text
from gc_core.tools.arguments_validation import tool_arguments_valid


def _function_calls(messages: Optional[List[Dict[str, Any]]]) -> List[Dict[str, Any]]:
    return [message for message in messages or [] if message.get("type") == "function_call"]


def important_tool_names(
    messages: Optional[List[Dict[str, Any]]],
    tools_data: Optional[Mapping[str, Any]],
    *,
    is_subagent: Callable[[str], bool],
) -> Tuple[str, ...]:
    """Имена важных тулов в ответе, в порядке появления, без дублей — материал
    решения о переигрыше и события лога. Важен вызов, чьё имя есть в реестре
    тулов такта И входит в общий список (`IMPORTANT_TOOL_NAMES`), либо это тул
    субагента (`is_subagent` — предикат контроллера, встроенные и кастомные),

    Имя вне реестра (снято `stop_tools`, выдумано моделью) важным не считается:
    это неизвестный тул и повод `invalid_call`."""
    registry = tools_data or {}
    names: List[str] = []
    for call in _function_calls(messages):
        name = str(call.get("name") or "")
        if name in names or name not in registry:
            continue
        if name in IMPORTANT_TOOL_NAMES or is_subagent(name):
            names.append(name)
    return tuple(names)


def has_invalid_call(
    messages: Optional[List[Dict[str, Any]]], tools_data: Optional[Mapping[str, Any]],
) -> bool:
    """Ответ несёт битый вызов: имени нет в реестре тулов такта либо аргументы не
    прошли бы валидацию `aapply_tool` (невалидный JSON, мимо схемы)."""
    registry = tools_data or {}
    for call in _function_calls(messages):
        name = str(call.get("name") or "")
        if name not in registry or not tool_arguments_valid(name, call.get("arguments"), registry):
            return True
    return False


def has_no_tool_call(messages: Optional[List[Dict[str, Any]]]) -> bool:
    """В ответе нет ни одного `function_call`: проза, пустой ответ либо
    reasoning-only (спасённый `<think>`)."""
    return not _function_calls(messages)


def discarded_answer(
    messages: Optional[List[Dict[str, Any]]],
) -> Tuple[List[Dict[str, str]], str]:
    """Выброшенный переигрышем ответ — для события лога: все его вызовы (имя и
    аргументы строкой из канона ответа, как прислала модель, в том числе битые) и
    текст ответа в том виде, в каком он вошёл бы в историю. Рассуждения модели
    сюда обычно не попадают: канон ответа их отрезает или держит отдельно от
    текста; `<think>` остаётся только у ответа из одних рассуждений. Материал
    сравнения с тем, что сделал уровень пола."""
    calls = [{"name": str(call.get("name") or ""), "arguments": str(call.get("arguments") or "")}
             for call in _function_calls(messages)]
    text = "\n".join(
        project_assistant_text(message.get("content"), strip_think=False).strip()
        for message in messages or []
        if message.get("type") == "message" and message.get("role") == "assistant"
    ).strip()
    return calls, text
