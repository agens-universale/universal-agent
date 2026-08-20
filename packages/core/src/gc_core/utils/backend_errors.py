"""Классификация ошибок бэкенда для восстановления в цикле агента."""
from __future__ import annotations

import requests
from typing import NamedTuple

from gc_core.utils.chat_completions_stream import BackendStreamError

_HTTP_RETRY_SKIP_STATUSES = {401, 403, 404}

# Маркеры переполнения контекстного окна модели. Провайдер (vLLM/OpenAI-совм.)
# просит уменьшить число токенов. Приходит как BackendStreamError (stream) или
# HTTPError/ValueError-400 (non-stream) — поэтому классифицируем по тексту.
# Провайдер-агностично: матчим общий доменный инвариант, а не точную фразу
# одного провайдера — gigacode («maximum context length»), vLLM («the model's
# context length»), OpenAI («context window») все содержат "context length"/
# "context window". Точечные фразы про токены — доп-страховка.
_CONTEXT_OVERFLOW_MARKERS = (
    "context length",
    "context window",
    "reduce the number of tokens",
    "please reduce the length",
)


class EmbeddedChoiceError(Exception):
    """Ошибка, зашитая провайдером-прокси внутрь HTTP-200 ответа
    (choices[0].error = {code, message, metadata}; стиль OpenRouter).

    Поверхностно запрос «успешен», поэтому транспорт её не ретраит, а фаталит
    она позже — в конвертации ответа, уже за границей retry-политики.
    Несём status_code (числовой из error.code), чтобы is_retriable_backend_error
    классифицировал её как обычную HTTP-ошибку бэкенда.

    raw_code — исходный error.code как есть (до попытки int()). Нужен, чтобы
    отличить нечисловой код (insufficient_quota/invalid_api_key: ошибка не
    HTTP-таймаута, повтором не чинится → НЕ ретраить) от отсутствия кода вовсе
    (finish_reason='error' с пустым выбором: транзиентный сбой → ретраить).
    Оба дают status_code=None, но раскладываются по-разному."""

    def __init__(self, message, *, status_code=None, metadata=None, raw_code=None):
        super().__init__(message)
        self.status_code = status_code
        self.metadata = metadata
        self.raw_code = raw_code


def is_auth_error(exc: BaseException) -> bool:
    """401 от бэкенда — истёкший/невалидный токен доступа. Лечится пере-выпуском
    токена (`auth_connector.get_token(force=True)`) + повтор запроса один раз
    (см. `_request_backend_canonical`). 403 сюда НЕ входит: forbidden рефрешем не
    чинится. Провайдер-агностично: HTTPError (non-stream) и BackendStreamError (stream)."""
    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    elif isinstance(exc, BackendStreamError):
        status_code = exc.status_code
    elif isinstance(exc, EmbeddedChoiceError):
        # Прокси завернул 401 в HTTP-200 (choices[0].error.code == 401):
        # это тот же истёкший токен — путь перевыпуска токена + один повтор,
        # а не адаптивный ретрай (401 в _HTTP_RETRY_SKIP_STATUSES).
        status_code = exc.status_code
    else:
        return False
    return status_code == 401


def is_retriable_backend_error(exc: BaseException) -> bool:
    """Ошибка бэкенда, для которой оправдан адаптивный ретрай (понижение max_tokens).

    Паритет stream ↔ non-stream: HTTP 4xx/5xx (кроме auth 401/403/404) ИЛИ
    стрим-ошибка (SSE error в 200-стриме) с таким же кодом. Нечисловой код
    (content_filter) → False."""
    if isinstance(exc, requests.exceptions.HTTPError):
        status_code = getattr(getattr(exc, "response", None), "status_code", None)
    elif isinstance(exc, BackendStreamError):
        status_code = exc.status_code
    elif isinstance(exc, EmbeddedChoiceError):
        # Embedded с ПРИСУТСТВУЮЩИМ нечисловым error.code (insufficient_quota,
        # invalid_api_key) — не HTTP-таймаут, повтором не чинится: 4 пере-отправки
        # промпта со сном и двойным списанием токенов бессмысленны → не ретраим.
        # Отсутствие кода вовсе (raw_code is None: finish_reason='error' с пустым
        # выбором) — транзиентный сбой, ретрай оправдан → пропускаем на общий путь.
        if exc.status_code is None and exc.raw_code is not None:
            return False
        status_code = exc.status_code
    else:
        return False
    if status_code in _HTTP_RETRY_SKIP_STATUSES:
        return False
    if status_code is None:
        return True
    try:
        return int(status_code) >= 400
    except (TypeError, ValueError):
        return False


def is_empty_completion(dct: dict) -> bool:
    """True, если ответ бэкенда генуинно пуст: нет content, нет tool_calls, нет
    finish_reason, нет error (ни top-level, ни choice-level). Напр. оборванный
    SSE-стрим, отдавший 0 токенов. Такой ответ → retriable (см. connect_backend)."""
    if not isinstance(dct, dict):
        return False
    if dct.get("error") is not None:
        return False
    choices = dct.get("choices")
    if not isinstance(choices, list) or not choices:
        return True
    ch0 = choices[0] if isinstance(choices[0], dict) else {}
    if ch0.get("error") is not None or ch0.get("finish_reason"):
        return False
    msg = ch0.get("message") or {}
    if msg.get("content"):
        return False
    if msg.get("tool_calls"):
        return False
    return True


class FinishDecision(NamedTuple):
    accept: bool   # принять ответ (не крашиться/не ретраить)
    unknown: bool  # finish_reason незнакомый → залогировать warning


_KNOWN_FINISH_REASONS = frozenset({"stop", "tool_calls", "length"})


def normalize_finish_reason(finish_reason, *, has_content: bool, has_tool_calls: bool) -> FinishDecision:
    """Permissive-но-честная трактовка finish_reason (провайдер-агностично).

    Известный {stop, tool_calls, length} → принять. Незнакомый/null + есть
    content/tool_calls → принять как «stop», но пометить unknown (warning).
    Незнакомый/null + пусто → не принять (генуинно непригодно → путь пустого)."""
    if finish_reason in _KNOWN_FINISH_REASONS:
        return FinishDecision(accept=True, unknown=False)
    if has_content or has_tool_calls:
        return FinishDecision(accept=True, unknown=True)
    return FinishDecision(accept=False, unknown=True)


def is_context_overflow_error(exc: BaseException) -> bool:
    """True, если ошибка бэкенда — переполнение контекстного окна модели.

    Восстановление в цикле: force-compaction истории + повтор запроса
    (см. run_agent). Проверяем текст исключения, т.к. тип различается
    (stream → BackendStreamError, non-stream → HTTPError/ValueError)."""
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)
