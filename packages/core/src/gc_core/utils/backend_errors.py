"""Классификация ошибок бэкенда для восстановления в цикле агента."""

from __future__ import annotations

import re

import httpx

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


def is_http_status_error(exc):
    return isinstance(exc, HTTP_STATUS_ERRORS)


def is_http_transport_error(exc):
    return isinstance(exc, HTTP_TRANSPORT_ERRORS)


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
    if is_image_request_error(exc):
        # Слепая модель или предел картинок — факт о модели, а не недоступность.
        # OpenRouter отвечает на картинку 404-м, и без этой проверки предохранитель
        # гасил бы алиас на всю сессию, а служебный вызов уходил бы в откат на
        # активную модель.
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
    _CONTEXT_OVERFLOW_MARKERS,
)
from gc_core.execution.finish_reason import (
    is_context_overflow_error as is_context_overflow_error,
)

# Отказ модели принять изображение (спека изображений §10): модель слепая или
# сервер поднят без зрения. Это факт о модели, а не сбой: повтор его не лечит, и
# недоступностью модели он не является. Маркеры — дословные фразы провайдеров в
# нижнем регистре. Размер тела (413, «too large») и форма запроса («Image URLs
# are only allowed for messages with role 'user'» — картинка не в user-сообщении,
# это наш баг) под маркеры не попадают: о зрении модели они ничего не говорят.
_IMAGE_REJECTION_MARKERS = (
    "at most 0 image(s) may be provided",  # vLLM: зрячая модель с --language-model-only
    "is not a multimodal model",  # vLLM: текстовая модель
    "image_url is only supported by certain models",  # OpenAI
    "no endpoints found that support image input",  # OpenRouter, HTTP 404
    "does not support image",  # общая форма: «…images» / «…image input»
    "unknown variant `image_url`",  # шлюзы на serde: части image_url не знают
    "only supports text input",  # litellm-прокси: «Model only supports text input»
)


def _rejection_text(exc: BaseException) -> str:
    """Текст отказа бэкенда: тело HTTP-ответа или сообщение исключения.

    Тело HTTP-отказа транспорт дочитывает до raise и в стрим-пути
    (`connect_backend.arequest_backend_chat_completions`, `_aread_error_text`):
    причина слепоты лежит только там, в статусе её нет. Тело не дочитано
    (обрыв на чтении) — текста нет."""
    if is_http_status_error(exc):
        try:
            return exc.response.text
        except httpx.ResponseNotRead:
            return ""
    if isinstance(exc, (BackendStreamError, EmbeddedChoiceError)):
        return str(exc)
    return ""


def is_image_rejection_error(exc: BaseException) -> bool:
    """True — бэкенд отказал, потому что модель не принимает изображения.

    Классифицирует только формы отказа бэкенда: HTTP-статус с телом, SSE-ошибку
    внутри 200-стрима, ошибку, зашитую прокси в HTTP-200. Проверяется во всех
    трёх слоях повторов — транспорт (`connect_backend`), адаптивная политика
    (`request_backend_policy`), предохранитель (`is_model_unavailable_error`):
    отказ не повторяется, не даёт «Ошибка LLM» и не гасит алиас на сессию.
    Что с ним делать (пометка модели, повтор без картинок) — решает контроллер.

    ``ModelProviderError`` (durable-исполнение) несёт классификацию, сделанную
    на воркере по исходному исключению."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.image_rejection
    text = _rejection_text(exc).lower()
    return any(marker in text for marker in _IMAGE_REJECTION_MARKERS)


# Бэкенд принимает не все картинки запроса: предел числа картинок на запрос (vLLM
# `--limit-mm-per-prompt`: «At most N image(s) may be provided in one prompt»; у
# облачных провайдеров — свои тексты) или тело запроса больше предела прокси (413,
# у nginx по умолчанию 1 МБ — меньше одного крупного скриншота в base64). Лечится
# меньшим числом картинок; сколько слать, решает контроллер
# (`vision.rejects.next_image_cap`). Предел должен говорить о самих картинках — слово о
# пределе рядом со словом «image(s)»: переполнение контекста у мультимодальной модели
# («…maximum model length… the number of image tokens depends on the number of
# images») с ним не совпадает, как и имя тула `fs_view_image` и картинка не в той роли.
_STATED_IMAGE_LIMIT = re.compile(
    r"\b(?:at most|no more than|up to|maximum of|max(?:imum)?)\s+(\d+)\s+images?\b"
)
_IMAGE_LIMIT_PHRASES = (
    _STATED_IMAGE_LIMIT,
    re.compile(r"\btoo many images?\b"),
    re.compile(r"\bimages?\b[^.]{0,40}?\b(?:limit|exceed(?:s|ed)?)\b"),
)
_IMAGE_LIMIT_STATUSES = frozenset({400, 422})
_PAYLOAD_TOO_LARGE = 413


def _error_status(exc: BaseException) -> int | None:
    """HTTP-код отказа бэкенда в любой его форме: HTTP-статус, SSE-ошибка в 200-стриме,
    ошибка, зашитая прокси в HTTP-200. Нечисловой код — None."""
    if is_http_status_error(exc):
        return http_status_code(exc)
    if isinstance(exc, (BackendStreamError, EmbeddedChoiceError)):
        try:
            return int(exc.status_code)
        except (TypeError, ValueError):
            return None
    return None


def _is_image_limit_error(exc: BaseException) -> bool:
    status = _error_status(exc)
    if status == _PAYLOAD_TOO_LARGE:
        return True
    if status not in _IMAGE_LIMIT_STATUSES:
        return False
    text = _rejection_text(exc).lower()
    if any(marker in text for marker in _CONTEXT_OVERFLOW_MARKERS):
        return (
            False  # переполнение контекста — путь сжатия истории, не картинок
        )
    return any(phrase.search(text) for phrase in _IMAGE_LIMIT_PHRASES)


def is_image_request_error(exc: BaseException) -> bool:
    """True — ошибка лечится меньшим числом картинок в запросе: модель не принимает их
    вовсе (`is_image_rejection_error`) или принимает не все (предел числа, размер тела).

    Проверяется во всех трёх слоях повторов — транспорт (`connect_backend`),
    адаптивная политика (`request_backend_policy`), предохранитель
    (`is_model_unavailable_error`): такая ошибка не повторяется тем же запросом, не
    даёт «Ошибка LLM» и не гасит алиас. Ни о чём не говорит без картинок в запросе —
    это проверяет контроллер (`request_carries_images`)."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.image_request
    return is_image_rejection_error(exc) or _is_image_limit_error(exc)


def image_limit_from_error(exc: BaseException) -> int | None:
    """Предел числа картинок на запрос, названный в тексте ошибки (vLLM: «At most N
    image(s)»), или None, если числа нет."""
    if isinstance(exc, ModelProviderError):
        return exc.failure.image_limit
    match = _STATED_IMAGE_LIMIT.search(_rejection_text(exc).lower())
    return int(match.group(1)) if match else None
