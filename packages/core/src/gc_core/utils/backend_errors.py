"""Классификация ошибок бэкенда для восстановления в цикле агента."""

from __future__ import annotations

from typing import NamedTuple

import httpx

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


#: Исключение «HTTP-статус не тот». Кортеж, а не один класс: форма пережила
#: время, когда транспортов было два (requests у тулов и MCP, httpx у модели),
#: и вызывающие полагаются на isinstance с кортежем.
HTTP_STATUS_ERRORS = (httpx.HTTPStatusError,)

#: Сетевые сбои, которые имеет смысл ПОВТОРЯТЬ: таймаут, обрыв, DNS, прокси.
#:
#: Раньше здесь стоял голый ``httpx.TransportError``, а он покрывает и то,
#: что повторять бессмысленно: ``UnsupportedProtocol`` (URL без схемы) и
#: ``LocalProtocolError`` (например не-latin1 в заголовке). То есть на
#: ОШИБКЕ КОНФИГУРАЦИИ предохранитель ставил алиас модели в «недоступна» на
#: всю сессию и перед этим честно ретраил 10/20/40/80 секунд. Докстрока
#: предохранителя при этом требовала обратного — «конфигурационные ошибки
#: наверх сразу».
#:
#: ``TimeoutException`` оставлен явно: он подкласс ``TransportError``, но
#: читается в списке.
HTTP_TRANSPORT_ERRORS = (
    httpx.TimeoutException,
    httpx.NetworkError,
    httpx.ProxyError,
    httpx.RemoteProtocolError,
)


def http_status_code(exc: BaseException) -> int | None:
    """Код HTTP-ошибки независимо от транспорта; None — это не HTTP-ошибка."""
    if isinstance(exc, HTTP_STATUS_ERRORS):
        return getattr(getattr(exc, "response", None), "status_code", None)
    return None


def is_auth_error(exc: BaseException) -> bool:
    """401 от бэкенда — истёкший/невалидный токен доступа. Лечится пере-выпуском
    токена (`auth_connector.get_token(force=True)`) + повтор запроса один раз
    (см. `_request_backend_canonical`). 403 сюда НЕ входит: forbidden рефрешем не
    чинится. Провайдер-агностично: HTTPError (non-stream) и BackendStreamError (stream)."""
    if isinstance(exc, HTTP_STATUS_ERRORS):
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
    if isinstance(exc, HTTP_STATUS_ERRORS):
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


def nonempty_reasoning_text(msg) -> str | None:
    """Канон ``reasoning_content`` или алиас OpenRouter ``reasoning``.

    Берём ОДНО из двух, не сумму — как стрим-ассемблер. Иначе нестрим
    (бенч/CLI/server) теряет reasoning-only ответ, который TUI спасает.
    Пробельный CoT → None; иначе возвращаем уже strip'нутый текст.
    """
    if not isinstance(msg, dict):
        return None
    for key in ("reasoning_content", "reasoning"):
        value = msg.get(key)
        if isinstance(value, str):
            stripped = value.strip()
            if stripped:
                return stripped
    return None


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
FINISH_KIND_STOP = "stop"
FINISH_KIND_TOOL_CALLS = "tool_calls"
FINISH_KIND_LENGTH = "length"
FINISH_KIND_BLOCKED = "blocked"  # content_filter / refusal / safety
FINISH_KIND_UNKNOWN = "unknown"

# Известные значения OpenAI-совместимых API → kind. Ключи — lower-case.
_KNOWN_FINISH_KINDS = {
    "stop": FINISH_KIND_STOP,
    "tool_calls": FINISH_KIND_TOOL_CALLS,
    "length": FINISH_KIND_LENGTH,
    "content_filter": FINISH_KIND_BLOCKED,
    "refusal": FINISH_KIND_BLOCKED,
    "safety": FINISH_KIND_BLOCKED,
}


class FinishDecision(NamedTuple):
    accept: bool  # принять payload в историю (не крашиться/не ретраить транспорт)
    unknown: bool  # finish_reason незнакомый → залогировать warning
    kind: str  # stop | tool_calls | length | blocked | unknown


def _finish_reason_key(finish_reason) -> str | None:
    if not isinstance(finish_reason, str):
        return None
    key = finish_reason.strip().lower()
    return key or None


def classify_finish_kind(finish_reason) -> str:
    """Одна классификация finish_reason для цикла агента и bounce-сообщений."""
    key = _finish_reason_key(finish_reason)
    if key is None:
        return FINISH_KIND_UNKNOWN
    return _KNOWN_FINISH_KINDS.get(key, FINISH_KIND_UNKNOWN)


def finish_blocks_final(finish_reason) -> bool:
    """Отказ провайдера: не принимать как финальный text answer (даже в allow)."""
    return classify_finish_kind(finish_reason) == FINISH_KIND_BLOCKED


def finish_prevents_text_final(finish_reason) -> bool:
    """Не принимать prose как финал хода: blocked (отказ) или length (усечение).

    Транспортный accept и запись в историю от этого не зависят — только
    решение text_response в run_agent (даже при text_answer_mode=allow).
    """
    kind = classify_finish_kind(finish_reason)
    return kind in (FINISH_KIND_BLOCKED, FINISH_KIND_LENGTH)


def normalize_finish_reason(
    finish_reason,
    *,
    has_content: bool,
    has_tool_calls: bool,
    has_reasoning: bool = False,
) -> FinishDecision:
    """Permissive-но-честная трактовка finish_reason (провайдер-агностично).

    Известный stop/tool_calls/length/blocked → принять, unknown=False.
    Незнакомый/null + есть content/tool_calls/reasoning → принять как payload,
    но пометить unknown (warning). Незнакомый/null + пусто → не принять
    (генуинно непригодно; конвертер отдаёт пустой батч → bounce, не
    транспортный ретрай).

    ``kind`` отделяет транспортный accept от политики цикла: blocked/length
    всё равно попадают в историю (если есть текст), но не становятся финалом
    (см. ``finish_prevents_text_final``).
    """
    key = _finish_reason_key(finish_reason)
    if key is not None and key in _KNOWN_FINISH_KINDS:
        return FinishDecision(accept=True, unknown=False, kind=_KNOWN_FINISH_KINDS[key])
    if has_content or has_tool_calls or has_reasoning:
        return FinishDecision(accept=True, unknown=True, kind=FINISH_KIND_UNKNOWN)
    return FinishDecision(accept=False, unknown=True, kind=FINISH_KIND_UNKNOWN)


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
    if isinstance(exc, AliasCircuitOpenError):
        return True
    if is_context_overflow_error(exc):
        return False
    # Транспорт здесь httpx, а не requests: предикат приехал с develop, где
    # ветка была синхронной. Спрашиваем общие кортежи модуля — они и заведены
    # ради того, чтобы правило не зависело от транспорта.
    if isinstance(exc, HTTP_STATUS_ERRORS):
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
    elif isinstance(exc, HTTP_TRANSPORT_ERRORS):
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


def is_context_overflow_error(exc: BaseException) -> bool:
    """True, если ошибка бэкенда — переполнение контекстного окна модели.

    Восстановление в цикле: force-compaction истории + повтор запроса
    (см. run_agent). Проверяем текст исключения, т.к. тип различается
    (stream → BackendStreamError, non-stream → HTTPError/ValueError)."""
    text = str(exc).lower()
    return any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS)
