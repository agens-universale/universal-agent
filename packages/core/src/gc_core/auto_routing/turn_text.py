"""Текст задачи хода и сообщения хода — одно правило для судей автовыбора (судья
уровня, судья пары). Чистые функции над State; модуль не тянет `gc_core.tools`."""

from __future__ import annotations

from typing import Any

from gc_core.intent_log.record import ACTION_USER_ASK
from gc_core.intent_log.serialize import deserialize_list
from gc_core.message_policy.user_message_policy import (
    extract_real_user_message_text,
)
from gc_core.utils.text_preview import head_tail_preview


def turn_messages(state: Any) -> list:
    """Сообщения ТЕКУЩЕГО хода: `state.messages` — вся история треда, а ход
    начинается с границы, которую ставит `State.mark_turn_start`. Границы нет
    (ход ещё ничего не записал, тестовые двойники) — читается вся история.

    Граница за концом истории — её переписала компакция (`summarize.compaction`):
    от прошлых ходов не осталось ничего, кроме технической сводки, поэтому
    читается весь остаток."""
    messages = list(getattr(state, "messages", None) or [])
    start = getattr(state, "turn_messages_start", None)
    if start is not None and int(start) <= len(messages):
        messages = messages[int(start) :]
    return [m for m in messages if isinstance(m, dict)]


def opening_user_text(state: Any) -> str:
    """Запасной источник задачи: запрос пользователя, открывший ход.
    Технические user-сообщения (напоминания, фидбек, нуджи) задачей не считаются —
    их отсекает `extract_real_user_message_text`."""
    for message in turn_messages(state):
        text = extract_real_user_message_text(message).strip()
        if text:
            return text
    return ""


def task_text(state: Any, *, limit: int, legacy: bool = False) -> str:
    """Все `user_ask` открытого хода через перевод строки (их бывает несколько:
    подгруженный прерванный прогон, сообщения посреди хода); журнал пуст — первое
    реальное сообщение хода. Начало и конец укладываются в `limit`."""
    records = deserialize_list(
        getattr(state, "current_user_intent", None) or []
    )
    asks = [
        r.content
        for r in records
        if getattr(r, "action", "") == ACTION_USER_ASK
    ]
    text = "\n".join(
        str(a).strip() for a in asks if isinstance(a, str) and a.strip()
    ).strip()
    text = text or opening_user_text(state)
    return text[:limit] if legacy else head_tail_preview(text, limit)
