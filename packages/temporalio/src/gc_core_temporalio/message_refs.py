"""Сообщения во входе activity модели — ссылками на уже переданные.

Вход каждой activity — запись истории Temporal. Запрос к модели несёт всю
переписку, и без ссылок история хода росла бы на весь контекст за шаг
(квадратично за ход, лимит записи 2 МБ). Workflow передаёт целиком только
сообщения, которых activity модели в этом запуске ещё не получала; остальные —
хэшем содержимого (~40 байт). Воркер держит полученные сообщения в памяти по
владельцу хода; на промахе (другой воркер, перезапуск) ищет их в треде
ThreadStore, а не найдя — отвечает ``ModelContextMissing``, и workflow
повторяет запрос с переданными целиком сообщениями.

Модуль общий для workflow и воркера: только ``hashlib`` и ``json``.
"""

from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Iterable

#: Ключ ссылки вместо сообщения: ``{"__ua_ref__": "<hash>"}``.
REF_KEY = "__ua_ref__"
#: Тип отказа activity: ссылки не найдены ни в памяти воркера, ни в треде.
CONTEXT_MISSING = "ModelContextMissing"


def message_hash(message: Any) -> str:
    """Хэш содержимого сообщения (порядок ключей не важен)."""
    encoded = json.dumps(
        message,
        sort_keys=True,
        ensure_ascii=False,
        separators=(",", ":"),
        default=str,
    ).encode()
    return hashlib.sha256(encoded).hexdigest()[:32]


def is_ref(value: Any) -> bool:
    return isinstance(value, dict) and len(value) == 1 and REF_KEY in value


class SentMessages:
    """Сторона workflow: какие сообщения activity модели уже получила.

    Состояние детерминировано: оно выводится из кода workflow и
    подтверждается только завершившейся activity.
    """

    def __init__(self) -> None:
        self._sent: set[str] = set()

    def encode(
        self, messages: Iterable[Any], *, also: Iterable[str] = ()
    ) -> tuple[list[Any], list[str]]:
        """→ (сообщения со ссылками, хэши переданных целиком).

        ``also`` — хэши, переданные целиком раньше в этом же запросе (батч
        повторяет хвост состояния): на них тоже ссылка.
        """
        sent = self._sent | set(also)
        out, inline = [], []
        for message in messages or []:
            digest = message_hash(message)
            if digest in sent:
                out.append({REF_KEY: digest})
            else:
                out.append(message)
                inline.append(digest)
        return out, inline

    def confirm(self, hashes: Iterable[str]) -> None:
        self._sent.update(hashes)

    def forget(self) -> None:
        self._sent.clear()


def refs_in(messages: Iterable[Any]) -> list[str]:
    return [m[REF_KEY] for m in messages or [] if is_ref(m)]


def decode(messages: Iterable[Any], known: dict[str, Any]) -> list[Any]:
    """Ссылки → копии сообщений из ``known`` (KeyError — ссылки нет).

    Копии: обработчик модели может менять сообщения запроса, а ``known`` —
    память воркера для следующих запросов хода.
    """
    return [
        deepcopy(known[m[REF_KEY]]) if is_ref(m) else m for m in messages or []
    ]


__all__ = [
    "CONTEXT_MISSING",
    "REF_KEY",
    "SentMessages",
    "decode",
    "is_ref",
    "message_hash",
    "refs_in",
]
