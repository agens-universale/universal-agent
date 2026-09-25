"""Active child input scope: mid-run user prose → executing subagent.

Parent AgentExecutor.add_message routes here when a child is registered.
Child run_agent drains via get_pending_messages (same contract as parent).

Скоуп живёт В КОНТЕКСТЕ ПРОГОНА, а не в глобали модуля
-------------------------------------------------------

Раньше здесь стояла одна переменная на процесс (``_active``) под локом. Она
теряла адресата двумя способами, и оба воспроизводятся:

* сервер держит МНОГО прогонов сразу (``AgentRunRegistry._runs``) и аккуратно
  находит нужный по ключу ``(system, task_id)`` — а ``add_message`` отдавал
  сообщение тому субагенту, кто зарегистрировался ПОСЛЕДНИМ, без единой
  проверки, чей это прогон. Достаточно, чтобы в процессе работал любой
  субагент, и сообщение чужой сессии уезжало ему;
* второй ``activate_child_input`` перезаписывал переменную безусловно, а на
  выходе снимал регистрацию целиком — первый субагент оставался работать
  НЕЗАРЕГИСТРИРОВАННЫМ, и его сообщения уходили родителю.

Теперь адресат — поле ``RunContext.child_input_scope``. Изоляция сессий стала
структурной: до чужого контекста просто нет ссылки. Ключей и реестра не нужно,
а значит нет и вопроса «что делать, если ключ не сошёлся»; время жизни записи
равно времени жизни прогона, поэтому течь нечему.

Вложенности не бывает: дочерний конфиг ставит ``use_local_subagents: False``,
так что в одном прогоне активен максимум один скоуп.
"""

from __future__ import annotations

import threading
from contextlib import contextmanager
from typing import Any, Callable, Dict, Iterator, List, Optional


class ChildInputScope:
    """Thread-safe pending queue for one executing subagent."""

    def __init__(self, *, parent_call_id: str) -> None:
        self.parent_call_id = parent_call_id or ""
        self._q_lock = threading.Lock()
        self._pending: List[Dict[str, Any]] = []
        self._on_enqueue: Optional[Callable[[Dict[str, Any]], None]] = None

    def set_on_enqueue(
        self, callback: Optional[Callable[[Dict[str, Any]], None]]
    ) -> None:
        self._on_enqueue = callback

    def enqueue(self, message: Dict[str, Any]) -> None:
        # Лок нужен здесь и только здесь: очередь наполняет ЧУЖОЙ поток (TUI,
        # обработчик запроса сервера), а разбирает её loop прогона.
        with self._q_lock:
            self._pending.append(message)
        cb = self._on_enqueue
        if cb is not None:
            try:
                cb(message)
            except Exception:
                pass

    def drain(self) -> List[Dict[str, Any]]:
        with self._q_lock:
            out = self._pending[:]
            self._pending.clear()
            return out


@contextmanager
def activate_child_input(
    ctx: Any, *, parent_call_id: str
) -> Iterator[ChildInputScope]:
    """Зарегистрировать этого ребёнка как адресата ввода ДЛЯ ЭТОГО прогона."""
    scope = ChildInputScope(parent_call_id=parent_call_id)
    ctx.child_input_scope = scope
    try:
        yield scope
    finally:
        # Снимаем только СВОЮ регистрацию: если поверх успел встать другой
        # скоуп, затирать его нельзя — иначе его сообщения уйдут родителю.
        if getattr(ctx, "child_input_scope", None) is scope:
            ctx.child_input_scope = None


def try_enqueue_user_message(ctx: Any, message: Dict[str, Any]) -> bool:
    """Отдать сообщение субагенту ЭТОГО прогона. True = принято ребёнком."""
    scope = getattr(ctx, "child_input_scope", None)
    if scope is None:
        return False
    scope.enqueue(message)
    return True


def is_child_input_active(ctx: Any) -> bool:
    return getattr(ctx, "child_input_scope", None) is not None


def active_parent_call_id(ctx: Any) -> Optional[str]:
    scope = getattr(ctx, "child_input_scope", None)
    if scope is None:
        return None
    pid = scope.parent_call_id
    return pid if pid else None


__all__ = [
    "ChildInputScope",
    "activate_child_input",
    "active_parent_call_id",
    "is_child_input_active",
    "try_enqueue_user_message",
]
