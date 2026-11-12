"""Провод хранилища тредов v2 между workflow и воркером.

Workflow говорит с хранилищем воркера контрактом ``ThreadStore`` v2
(``gc_core.threads.store``): каждая операция — activity
``universal_agent.thread.<операция>``. Вход и выход activity — запись истории
Temporal (лимит 2 МБ на запись, 50 МБ на историю), поэтому:

* большой ответ (запись треда, поток, архив) идёт кусками текста JSON по
  ``PAGE_BYTES``; следующие куски — той же версии ответа, иначе
  ``ThreadChanged`` и чтение заново;
* запись треда, которую workflow уже видел, приходит разницей к ней
  (воркер держит её по отпечатку), не изменившаяся — признаком;
* состояние сессии в ``commit`` уходит разницей к состоянию на
  ``expected_revision``; воркер собирает его из записи хранилища, а не
  собрав — ``BaseMismatch``, и workflow шлёт состояние целиком.

Модуль общий для workflow и воркера: dataclass, исключения и чистые функции.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from gc_core.threads.errors import ThreadStoreError

from gc_core_temporalio.meta_delta import normalize

#: Потолок куска ответа (байты JSON). Результат activity — запись истории
#: Temporal (лимит 2 МБ, предупреждение сервера с 512 КБ): длинный тред
#: читается несколькими activities, каждая ниже порога предупреждения.
PAGE_BYTES = 400 * 1024


class BaseMismatch(ThreadStoreError):
    """Воркер не собрал из разницы то состояние, что записал workflow (в
    хранилище другая ревизия: повтор после записи, чужая запись). Workflow
    повторяет запись с состоянием целиком."""


class ThreadChanged(ThreadStoreError):
    """Ответ изменился между кусками (тред записали) — читать заново."""


def strip_fetch_cache(state: Any) -> Any:
    """Состояние сессии (JSON) для провода: непустой кэш страниц
    ``fetch_url`` — пустым. Кэш под Temporal живёт в памяти воркера, а поле
    остаётся заданным — форма meta не меняется."""
    if isinstance(state, dict) and state.get("fetch_url_cache"):
        return {**state, "fetch_url_cache": {}}
    return state


#: Части записи (``ThreadChange``/``SideChange``), заменяющие поле записи
#: треда целиком: в проводе они идут разницей к известной записи.
RECORD_PARTS = {
    "set_state": "state",
    "set_usage": "usage",
    "set_info": "info",
    "set_runtime": "runtime",
    "set_extra": "extra",
}


def expected_record(base: dict, change: dict) -> dict:
    """Запись треда, какой её ждёт workflow после своей записи: известная
    ``base`` с частями ``change`` (JSON провода) и правкой состояния
    фронтенда. Поля хранилища (ревизия, штампы, счётчики, отметки потоков)
    придут разницей."""
    expected = dict(base)
    for part, field_name in RECORD_PARTS.items():
        if change.get(part) is not None:
            value = normalize(change[part])
            if field_name == "state":
                value = strip_fetch_cache(value)
            expected[field_name] = value
    frontend = dict(expected.get("frontend") or {})
    if change.get("set_frontend") is not None:
        name, state = change["set_frontend"]
        frontend[name] = normalize(state)
    if change.get("patch_frontend") is not None:
        name, patch = change["patch_frontend"]
        frontend[name] = {**(frontend.get(name) or {}), **normalize(patch)}
    if frontend or "frontend" in expected:
        expected["frontend"] = frontend
    return expected


def lease_owner(workflow_id: str, chain: str) -> str:
    """Владелец аренды треда — цепочка run сессии (``first_execution_run_id``):
    Continue-As-New его не меняет, новый запуск с тем же workflow ID —
    другой владелец (новая эпоха, запись брошенной цепочки отсекается)."""
    return f"{workflow_id}#{chain}"


def parse_lease_owner(owner: str) -> tuple[str, str | None]:
    """``(workflow ID, цепочка run | None)``; без ``#`` — не workflow
    (локальный процесс) или цепочка неизвестна."""
    workflow_id, sep, chain = owner.rpartition("#")
    return (workflow_id, chain) if sep else (owner, None)


@dataclass
class ThreadCall:
    """Вызов операции хранилища ``store_id`` над тредом ``(system,
    thread)``; ``args`` — аргументы операции (JSON)."""

    store_id: str
    system: str
    thread: str
    args: dict[str, Any] = field(default_factory=dict)


@dataclass
class ThreadReply:
    """Ответ: маленький — ``value``; большой — кусок ``chunk`` текста JSON,
    ``next`` — позиция следующего (``None`` — последний), ``version`` —
    версия ответа целиком. ``unchanged`` — запись совпала с известной
    workflow; ``delta`` — разница к ней."""

    value: Any = None
    chunk: str | None = None
    next: int | None = None
    version: str | None = None
    unchanged: bool = False
    delta: Any = None
