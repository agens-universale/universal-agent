"""Безопасный HTTP-контракт ошибок хода; текст исключения остаётся в логе."""

import httpx
from gc_core.execution.contracts import (
    ContextWindowExceeded,
    MainModelUnavailable,
    ModelProviderError,
    PayloadTooLarge,
)
from gc_core.utils.backend_errors import (
    BackendBadResponseError,
    EmbeddedChoiceError,
)
from gc_core.utils.chat_completions_stream import BackendStreamError

from gc_server.chat.errors import ApiError

_ERRORS = {
    "upstream_authentication_error": (
        502,
        "Model service rejected its configured credentials.",
    ),
    "upstream_permission_denied": (502, "Model service denied access."),
    "upstream_timeout": (504, "Model service timed out."),
    "upstream_rate_limited": (
        503,
        "Model service rate limit reached. Try again later.",
    ),
    "upstream_unavailable": (
        503,
        "Model service is unavailable. Try again later.",
    ),
    "upstream_invalid_response": (
        502,
        "Model service returned an invalid response.",
    ),
    "upstream_error": (502, "Model service could not complete the request."),
    "server_error": (500, "Internal server error."),
    "response_persistence_failed": (
        503,
        "Response could not be saved. Do not repeat the request; check its status later.",
    ),
    "context_length_exceeded": (
        400,
        "Conversation exceeds the model context window. Start a shorter conversation.",
    ),
    "request_too_large": (
        413,
        "Request is too large for the execution backend. Reduce its size.",
    ),
}
_STATUSES = {
    401: "upstream_authentication_error",
    403: "upstream_permission_denied",
    429: "upstream_rate_limited",
}


def _status_code(status: object) -> str:
    # SSE error.code бывает именем ошибки, а не HTTP-статусом.
    try:
        status = int(status)
    except (TypeError, ValueError, OverflowError):
        return "upstream_error"
    if status in _STATUSES:
        return _STATUSES[status]
    if status >= 500:
        return "upstream_unavailable"
    return "upstream_error"


def _provider_code(exc: ModelProviderError) -> str:
    failure = exc.failure
    if failure.auth_error:
        return "upstream_authentication_error"
    if "Timeout" in failure.error_type:
        return "upstream_timeout"
    if failure.error_type == "BackendBadResponseError":
        return "upstream_invalid_response"
    status = failure.details.get("status_code")
    if isinstance(status, int):
        return _status_code(status)
    return "upstream_unavailable" if failure.unavailable else "upstream_error"


def _code(exc: BaseException) -> str:
    if isinstance(exc, ContextWindowExceeded):
        return "context_length_exceeded"
    if isinstance(exc, PayloadTooLarge):
        return "request_too_large"
    if isinstance(exc, ModelProviderError):
        return _provider_code(exc)
    if isinstance(exc, httpx.TimeoutException):
        return "upstream_timeout"
    if isinstance(exc, BackendBadResponseError):
        return "upstream_invalid_response"
    if isinstance(exc, httpx.HTTPStatusError):
        return _status_code(exc.response.status_code)
    if isinstance(exc, (BackendStreamError, EmbeddedChoiceError)):
        return _status_code(exc.status_code)
    if isinstance(
        exc,
        (httpx.NetworkError, httpx.RemoteProtocolError, MainModelUnavailable),
    ):
        return "upstream_unavailable"
    return "server_error"


def model_api_error(exc: BaseException) -> ApiError:
    if isinstance(exc, ApiError):
        return exc
    code = _code(exc)
    status, message = _ERRORS[code]
    return ApiError(status, code, message)


def error_http_status(payload: dict) -> int:
    """Один HTTP-статус для сохранённого Responses и конверта Completions."""
    error = payload.get("error") or {}
    return _ERRORS.get(error.get("code"), _ERRORS["server_error"])[0]
