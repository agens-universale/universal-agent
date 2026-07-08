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
_listeners: List[Callable[[], None]] = []

_REQUEST_LABEL_RU = {
    "main_agent": "основной агент",
    "chat_completion": "вспомогательный LLM",
    "intent_guard_finish_gate": "судья (finish gate)",
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
        if listeners:
            _listeners.clear()


def collect_runtime_notices() -> List[Notice]:
    with _lock:
        return list(_notices)


def push_runtime_notice(notice: Notice) -> None:
    with _lock:
        for existing in _notices:
            if existing.code == notice.code and existing.message == notice.message:
                return
        _notices.append(notice)
        if len(_notices) > _MAX_NOTICES:
            del _notices[: len(_notices) - _MAX_NOTICES]
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
