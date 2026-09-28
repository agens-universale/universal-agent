"""Разметка prompt cache для claude-моделей — адаптация проводного формата.

Провайдер claude-моделей кэширует префикс запроса только по явной разметке
``cache_control``. Живые пробы на OpenRouter и routerai (OpenAI-совместимый
прокси) показали: метки на текстовых блоках сообщений проходят через оба, а
поле ``cache_control`` верхнего уровня — только через OpenRouter и ничего не
добавляет там, где уже стоят обе метки; для остальных OpenAI-совместимых
прокси не проверялось. Поэтому здесь — только явная разметка, без поля: метка
A на первом сообщении роли ``system`` (вместе с ним кэшируется префикс
инструментов — у провайдера порядок tools → system → messages) и метка B на
последнем сообщении роли ``user``/``tool`` — скользящая точка хвоста истории,
следующий вызов находит предыдущую позицию окном поиска. Настройка
``backend.claude_prompt_cache`` решает, размечать ли: не задана (``None``) —
автодетект по id модели (подстрока ``claude`` в любой форме имени:
``anthropic/claude-…``, ``claude-…@…``, ``us.anthropic.claude-…``); булево
значение — принудительно вкл/выкл. Здесь — чистые функции без State и
конфига: слой утилит, тот же класс, что выбор ``max_tokens`` по
``use_max_tokens_param``. Спека: docs/specs/2026-09-07-prompt-cache-design.md.
"""

from __future__ import annotations

from typing import Any

# TTL по умолчанию у провайдера claude-моделей — 5 минут; ``ttl: "1h"`` не используем (запись 2×).
CACHE_CONTROL: dict[str, str] = {"type": "ephemeral"}

_MODEL_MARKER = "claude"
_TAIL_ROLES = ("user", "tool")


def is_prompt_cache_model(model_id: Any) -> bool:
    """Модель кэшируется по явным меткам: провайдерский id содержит ``claude``."""
    return isinstance(model_id, str) and _MODEL_MARKER in model_id.lower()


def prompt_cache_enabled(setting: Any, model_id: Any) -> bool:
    """Нужна ли разметка этому вызову. Булево или целое (из схемы — bool; из
    CLI-оверрайдов и agent_overrides сервера — мимо схемы, yaml: 0/1, false/true)
    решает само; всё остальное — ключ не задан или нераспознанная строка — автодетект
    по id модели: незнакомое слово никогда не включает разметку принудительно."""
    if isinstance(setting, int):  # bool — подкласс int
        return bool(setting)
    return is_prompt_cache_model(model_id)


def _is_candidate(message: Any) -> bool:
    """Сообщение можно пометить: ``content`` — строка с непробельным текстом.
    Провайдер claude-моделей отвергает пустой и пробельный текстовый блок."""
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return isinstance(content, str) and bool(content.strip())


def _with_cache_control(message: dict) -> dict:
    """Копия сообщения, где строка контента стала одной текстовой частью с меткой.
    Остальные ключи (``name``, ``tool_call_id`` и будущие) сохраняются как есть."""
    marked = dict(message)
    marked["content"] = [
        {
            "type": "text",
            "text": message["content"],
            "cache_control": dict(CACHE_CONTROL),
        }
    ]
    return marked


def _mark_last_tail_candidate(messages: list) -> list:
    """Метка B — последнее сообщение роли user/tool с непробельным текстом (скользящая
    точка: провайдер кэширует префикс до неё, следующий вызов читает его)."""
    marked = list(messages)
    for index in range(len(marked) - 1, -1, -1):
        message = marked[index]
        if (
            isinstance(message, dict)
            and message.get("role") in _TAIL_ROLES
            and _is_candidate(message)
        ):
            marked[index] = _with_cache_control(message)
            break
    return marked


def apply_prompt_cache(payload: dict) -> dict:
    """Новый payload с двумя явными метками (спека §4.3, волна 2).

    A — на первом сообщении роли ``system``, если оно кандидат: префикс кэша идёт
    tools → system → messages, одна метка покрывает описания инструментов и промпт.
    B — на последнем сообщении роли user/tool: скользит с каждым вызовом, предыдущую
    позицию провайдер находит окном поиска (серия результатов инструментов — одна
    позиция). Поля верхнего уровня нет: часть-уровневые метки проходят через все
    проверенные прокси, поле — только через OpenRouter. Вход не мутирует;
    непомеченные сообщения — те же объекты.
    """
    messages = payload.get("messages")
    if not isinstance(messages, list):
        return dict(payload)
    marked = list(messages)
    for index, message in enumerate(marked):
        if isinstance(message, dict) and message.get("role") == "system":
            if _is_candidate(message):
                marked[index] = _with_cache_control(message)
            break
    marked = _mark_last_tail_candidate(marked)
    result = dict(payload)
    result["messages"] = marked
    return result
