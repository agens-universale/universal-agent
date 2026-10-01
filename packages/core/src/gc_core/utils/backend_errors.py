"""Классификация ошибок бэкенда для восстановления в цикле агента."""

from __future__ import annotations

from gc_core.execution.contracts import ModelProviderError
from gc_core.execution.finish_reason import (
    nonempty_reasoning_text as nonempty_reasoning_text,
)
from gc_core.model_router import AliasCircuitOpenError
from gc_core.utils.chat_completions_stream import BackendStreamError

_HTTP_RETRY_SKIP_STATUSES = {401, 403, 404}

# Маркеры переполнения контекстного окна модели. Провайдер (vLLM/OpenAI-совм.)
# просит уменьшить число токенов. Приходит как BackendStreamError (stream) или
# HTTPError/ValueError-400 (non-stream) — поэтому классифицируем по тексту.
# Провайдер-агностично: матчим общий доменный инвариант, а не точную фразу
# одного провайдера — gigacode («maximum context length»), vLLM («the model's
# context length»), OpenAI («context window») все содержат "context length"/
# "context window". Точечные фразы про токены — доп-страховка.


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

    def __init__(
        self, message, *, status_code=None, metadata=None, raw_code=None
    ):
        super().__init__(message)
        self.status_code = status_code
        self.metadata = metadata
        self.raw_code = raw_code


def _http_error_types(name):
    # Transport classes are needed only on the worker side. Core workflow
    # policy consumes ModelProviderError and must not import an HTTP client.
    import httpx

    if name == "HTTP_STATUS_ERRORS":
        return (httpx.HTTPStatusError,)
    return (
        httpx.TimeoutException,
        httpx.NetworkError,
        httpx.ProxyError,
        httpx.RemoteProtocolError,
    )


def __getattr__(name):
    if name in {"HTTP_STATUS_ERRORS", "HTTP_TRANSPORT_ERRORS"}:
        return _http_error_types(name)
    raise AttributeError(name)


def _is_http_error(exc, name):
    # Preserve isinstance semantics (including user subclasses), while
    # domain errors and unrelated exceptions never load worker HTTP modules.
    if not any(
        cls.__module__.split(".", 1)[0] == "httpx" for cls in type(exc).__mro__
    ):
        return False
    return isinstance(exc, _http_error_types(name))


def is_http_status_error(exc):
    return _is_http_error(exc, "HTTP_STATUS_ERRORS")


def is_http_transport_error(exc):
    return _is_http_error(exc, "HTTP_TRANSPORT_ERRORS")


def is_previous_response_cache_miss(exc: BaseException) -> bool:
    """Classify a lost Responses cache without exposing HTTP data to workflow."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.response_cache_miss
    text = str(exc).lower()
    if is_http_status_error(exc):
        # Transport has already read the error body; no I/O occurs here.
        try:
            text += " " + exc.response.text.lower()
        except Exception:
            pass
    return (
        "previous_response_id" in text
        and ("not found" in text or "cache" in text)
    ) or ("cache" in text and "not found" in text)


def http_status_code(exc: BaseException) -> int | None:
    """Код HTTP-ошибки независимо от транспорта; None — это не HTTP-ошибка."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.details.get("status_code")
    if is_http_status_error(exc):
        return getattr(getattr(exc, "response", None), "status_code", None)
    return None


def is_auth_error(exc: BaseException) -> bool:
    """401 от бэкенда — истёкший/невалидный токен доступа. Лечится пере-выпуском
    токена (`auth_connector.get_token(force=True)`) + повтор запроса один раз
    (см. `_request_backend_canonical`). 403 сюда НЕ входит: forbidden рефрешем не
    чинится. Провайдер-агностично: HTTPError (non-stream) и BackendStreamError (stream)."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.auth_error
    if is_http_status_error(exc):
        status_code = http_status_code(exc)
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
    if isinstance(exc, ModelProviderError):
        return exc.failure.retryable
    if is_http_status_error(exc):
        status_code = http_status_code(exc)
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


def has_payload(msg) -> bool:
    """True, если в message есть нагрузка: content, tool_calls или reasoning.

    content — truthiness как у транспорта (пробел/перевод строки — не пустой
    стрим: цикл bounce «нужна тула», не ретрай completion). Reasoning —
    nonempty после strip: пустой CoT не salvage.
    """
    if not isinstance(msg, dict):
        return False
    if msg.get("content") or msg.get("tool_calls"):
        return True
    return nonempty_reasoning_text(msg) is not None


def is_empty_completion(dct: dict) -> bool:
    """True, если ответ бэкенда генуинно пуст: нет content, нет tool_calls, нет
    reasoning_content, нет finish_reason, нет error (ни top-level, ни choice-level).
    Напр. оборванный SSE-стрим, отдавший 0 токенов. Такой ответ → retriable
    (см. connect_backend). Непустой reasoning — не пустой стрим (salvage, не ретрай).
    Пробельный content — не пустой: payload есть, ретрай не нужен."""
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
    return not has_payload(ch0.get("message") or {})


# Семантика finish_reason для транспорта (accept) и политики цикла (bounce/final).
from gc_core.execution.finish_reason import (
    _KNOWN_FINISH_KINDS as _KNOWN_FINISH_KINDS,
)
from gc_core.execution.finish_reason import (
    FINISH_KIND_BLOCKED as FINISH_KIND_BLOCKED,
)
from gc_core.execution.finish_reason import (
    FINISH_KIND_LENGTH as FINISH_KIND_LENGTH,
)
from gc_core.execution.finish_reason import (
    FINISH_KIND_STOP as FINISH_KIND_STOP,
)
from gc_core.execution.finish_reason import (
    FINISH_KIND_TOOL_CALLS as FINISH_KIND_TOOL_CALLS,
)
from gc_core.execution.finish_reason import (
    FINISH_KIND_UNKNOWN as FINISH_KIND_UNKNOWN,
)
from gc_core.execution.finish_reason import (
    FinishDecision as FinishDecision,
)
from gc_core.execution.finish_reason import (
    _finish_reason_key as _finish_reason_key,
)
from gc_core.execution.finish_reason import (
    classify_finish_kind as classify_finish_kind,
)
from gc_core.execution.finish_reason import (
    finish_blocks_final as finish_blocks_final,
)
from gc_core.execution.finish_reason import (
    finish_prevents_text_final as finish_prevents_text_final,
)
from gc_core.execution.finish_reason import (
    normalize_finish_reason as normalize_finish_reason,
)


def is_model_unavailable_error(exc: BaseException) -> bool:
    """«Недоступна» модель (спека §8): сетевые ошибки, 5xx, 401/403, 404 —
    после исчерпания существующих ретраев (transport + adaptive max_tokens +
    401-refresh) — либо алиас уже открыт в предохранителе (`AliasCircuitOpenError`,
    та же недоступность, просто уже подтверждённая другим вызовом этой сессии).
    Решает, ставить ли алиас в `model_router.CircuitBreaker` — не путать с
    `is_retriable_backend_error` (годится ли ошибка для повторной попытки ВНУТРИ
    текущего запроса, до исчерпания ретраев).

    Сетевые — именно `Timeout`/`ConnectionError` (со всеми их подклассами: SSLError,
    ProxyError, ConnectTimeout, ReadTimeout), а НЕ общий `RequestException`: тот
    накрывает и битый ответ живого провайдера (`JSONDecodeError`), и кривой
    url/прокси (`MissingSchema`, `InvalidURL`, `TooManyRedirects`) — от таких
    предохранитель гасил бы алиас на всю сессию, хотя модель жива."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.unavailable
    if isinstance(exc, AliasCircuitOpenError):
        return True
    if is_context_overflow_error(exc):
        return False
    # Транспорт здесь httpx, а не requests: предикат приехал с develop, где
    # ветка была синхронной. Спрашиваем общие кортежи модуля — они и заведены
    # ради того, чтобы правило не зависело от транспорта.
    if is_http_status_error(exc):
        status_code = http_status_code(exc)
    elif isinstance(exc, BackendStreamError):
        status_code = exc.status_code
    elif isinstance(exc, EmbeddedChoiceError):
        # Прокси кладёт отказ ВНУТРЬ HTTP-200 (choices[0].error / top-level error):
        # снаружи запрос «успешен», и без этой ветки тот же самый отказ модели —
        # 5xx, 401, 404 — не считался бы недоступностью только из-за обёртки.
        # status_code=None (нечисловой код вроде insufficient_quota либо код
        # отсутствует вовсе) уходит на общий путь ниже и даёт False: гасить алиас
        # на всю сессию по неизвестной причине нельзя.
        status_code = exc.status_code
    elif is_http_transport_error(exc):  # noqa: SIM103  # ветка с обоснованием в цепочке elif
        # Только транспортные сбои (таймаут, обрыв, DNS), а не любой сбой
        # клиента: от битого url или лишних редиректов предохранитель гасил бы
        # алиас на всю сессию, хотя модель жива. У httpx это ровно
        # TransportError/TimeoutException — см. кортеж выше.
        return True
    else:
        return False
    if status_code is None:
        return False
    if status_code in (401, 403, 404):
        return True
    try:
        return int(status_code) >= 500
    except (TypeError, ValueError):
        return False


from gc_core.execution.finish_reason import (
    is_context_overflow_error as is_context_overflow_error,
)
