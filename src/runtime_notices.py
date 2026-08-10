"""Эфемерные runtime-уведомления (TUI): ошибки LLM и прочие не-блокирующие события.

В отличие от startup_notices, живут только в памяти текущего процесса;
не пишутся в meta/jsonl. TUI показывает их в карточке и на экране /notices.
"""
from __future__ import annotations

import json
import threading
from typing import Callable, List, Optional

import requests

from startup_notices import Notice
from utils.chat_completions_stream import BackendStreamError

_MAX_NOTICES = 50
_lock = threading.Lock()
_notices: List[Notice] = []
# Индекс в `_notices`, с которого начинаются непрочитанные runtime-уведомления.
# После просмотра `/notices` индекс продвигается в конец списка, делая
# непрочитанные пустыми (бейджи гаснут).
_unread_from_index: int = 0
_listeners: List[Callable[[], None]] = []

_REQUEST_LABEL_RU = {
    "main_agent": "основной агент",
    "chat_completion": "вспомогательный LLM",
    "intent_guard_finish_gate": "судья (finish gate)",
    "intent_guard_finish_gate_retry": "судья (finish gate, retry)",
    "subagent_special_agent": "special agent (генерация роли)",
}


def _label_ru(request_label: Optional[str]) -> str:
    label = (request_label or "").strip()
    if not label:
        return "LLM"
    if label in _REQUEST_LABEL_RU:
        return _REQUEST_LABEL_RU[label]
    if label.startswith("intent_scope:"):
        return "intent scope"
    if label.startswith("summary"):
        return "суммаризация"
    return label


def register_runtime_notices_listener(callback: Callable[[], None]) -> None:
    """Подписка на новое уведомление (TUI: обновить карточку /notices)."""
    with _lock:
        if callback not in _listeners:
            _listeners.append(callback)


def clear_runtime_notices(*, listeners: bool = False) -> None:
    """Сброс (тесты / новая сессия). listeners=True — снять подписки TUI."""
    with _lock:
        _notices.clear()
        global _unread_from_index
        _unread_from_index = 0
        if listeners:
            _listeners.clear()


def collect_runtime_notices() -> List[Notice]:
    with _lock:
        return list(_notices)


def collect_unread_runtime_notices() -> List[Notice]:
    with _lock:
        if _unread_from_index <= 0:
            return list(_notices)
        if _unread_from_index >= len(_notices):
            return []
        return list(_notices[_unread_from_index:])


def mark_runtime_notices_seen() -> None:
    """Пометить текущие runtime-уведомления как просмотренные.

    Это гасит бейджи (счётчики) в footer-left/footer-right и карточке TUI,
    но сами уведомления остаются доступными на экране `/notices`.
    """
    global _unread_from_index
    listeners: List[Callable[[], None]] = []
    changed = False
    with _lock:
        unread_now = len(_notices) - _unread_from_index
        if unread_now <= 0:
            return
        _unread_from_index = len(_notices)
        changed = True
        listeners = list(_listeners)
    if changed:
        for cb in listeners:
            try:
                cb()
            except Exception:
                pass


def push_runtime_notice(notice: Notice) -> None:
    with _lock:
        for existing in _notices:
            if existing.code == notice.code and existing.message == notice.message:
                return
        _notices.append(notice)
        if len(_notices) > _MAX_NOTICES:
            removed = len(_notices) - _MAX_NOTICES
            del _notices[:removed]
            # Сдвигаем точку “последнего просмотра”, т.к. начало списка обрезали.
            global _unread_from_index
            _unread_from_index = max(0, _unread_from_index - removed)
        listeners = list(_listeners)
    for cb in listeners:
        try:
            cb()
        except Exception:
            pass


def _extract_http_detail(exc: BaseException) -> tuple[Optional[int], str]:
    status: Optional[int] = None
    body = ""
    if isinstance(exc, requests.exceptions.HTTPError):
        resp = getattr(exc, "response", None)
        status = getattr(resp, "status_code", None)
        try:
            body = (resp.text or "") if resp is not None else ""
        except Exception:
            body = ""
    elif isinstance(exc, BackendStreamError):
        status = exc.status_code
        body = str(exc)
    else:
        body = str(exc)

    detail = ""
    if body:
        try:
            parsed = json.loads(body)
            if isinstance(parsed, dict):
                detail = str(
                    parsed.get("message")
                    or parsed.get("error")
                    or parsed.get("detail")
                    or ""
                ).strip()
        except json.JSONDecodeError:
            detail = body.strip()
    if not detail:
        detail = str(exc).strip()
    if len(detail) > 280:
        detail = detail[:277] + "..."
    return status, detail


def notice_from_llm_failure(
    exc: BaseException,
    *,
    request_label: Optional[str] = None,
    url: Optional[str] = None,
) -> Notice:
    """Собрать Notice из исключения backend chat/completions."""
    status, detail = _extract_http_detail(exc)
    who = _label_ru(request_label)
    if status is not None:
        headline = f"Ошибка LLM ({who}): HTTP {status}."
    else:
        headline = f"Ошибка LLM ({who})."
    message = f"{headline} {detail}" if detail else headline
    highlights: list[str] = []
    if request_label:
        highlights.append(request_label)
    if url:
        highlights.append(url)
    for token in ("reasoning_effort", "max_tokens", "context length"):
        if token in detail:
            highlights.append(token)
    code = f"llm_error_{status}" if status is not None else "llm_error"
    return Notice("warning", code, message, tuple(dict.fromkeys(highlights)))


def maybe_push_llm_failure_notice(
    exc: BaseException,
    *,
    request_label: Optional[str] = None,
    url: Optional[str] = None,
    enabled: bool = True,
) -> None:
    if not enabled:
        return
    push_runtime_notice(
        notice_from_llm_failure(exc, request_label=request_label, url=url)
    )


def notice_from_fallback_to_active(
    *,
    who: str,
    failed_alias: str,
    active_model: str,
    mode: str = "",
) -> Notice:
    """Класс 1 фоллбэков недоступности (спека §8): служебный потребитель или
    дочерний субагент с пином деградировал на активную модель после отказа
    модели привязки. ``mode`` — доп. пометка ("рестарт"/"деградация") для
    субагентов; пусто для служебных потребителей."""
    suffix = f" ({mode})" if mode else ""
    message = f"{who}{suffix}: {failed_alias} недоступна — выполнено активной {active_model}"
    return Notice(
        "warning", "fallback_to_active", message, (failed_alias, active_model)
    )


def maybe_push_fallback_to_active_notice(
    *,
    who: str,
    failed_alias: str,
    active_model: str,
    mode: str = "",
    enabled: bool = True,
) -> None:
    if not enabled:
        return
    push_runtime_notice(
        notice_from_fallback_to_active(
            who=who, failed_alias=failed_alias, active_model=active_model, mode=mode
        )
    )
