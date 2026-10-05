"""Worker-only provider error encoding; no HTTP objects enter durable history."""

import httpx
from gc_core.execution.contracts import ExecutionFailure, ModelFailure
from gc_core.model_router import AliasCircuitOpenError
from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    image_limit_from_error,
    is_auth_error,
    is_context_overflow_error,
    is_image_rejection_error,
    is_image_request_error,
    is_model_unavailable_error,
    is_previous_response_cache_miss,
    is_retriable_backend_error,
)
from gc_core.utils.chat_completions_stream import BackendStreamError


def _provider_failure(exc, message, details=None):
    return ModelFailure(
        kind="provider",
        error_type=type(exc).__name__,
        message=message,
        details=details or {},
        auth_error=is_auth_error(exc),
        retryable=is_retriable_backend_error(exc),
        unavailable=is_model_unavailable_error(exc),
        context_overflow=is_context_overflow_error(exc),
        response_cache_miss=is_previous_response_cache_miss(exc),
        image_rejection=is_image_rejection_error(exc),
        image_request=is_image_request_error(exc),
        image_limit=image_limit_from_error(exc),
    )


def encode_model_failure(exc: Exception) -> ModelFailure:
    """Classify supported errors; never serialize a live provider exception."""
    name = type(exc).__name__
    if isinstance(exc, ExecutionFailure):
        return ModelFailure("execution", name, str(exc))
    if isinstance(exc, httpx.HTTPStatusError):
        status = exc.response.status_code
        # str(HTTPStatusError) may contain a credential-bearing request URL.
        message = (
            "Model context window exceeded"
            if is_context_overflow_error(exc)
            else f"Model HTTP {status}"
        )
        return _provider_failure(exc, message, {"status_code": status})
    if isinstance(exc, httpx.TransportError):
        return _provider_failure(exc, name)
    if isinstance(exc, EmbeddedChoiceError):
        return _provider_failure(
            exc,
            str(exc),
            {"status_code": exc.status_code, "raw_code": exc.raw_code},
        )
    if isinstance(exc, BackendStreamError):
        return _provider_failure(
            exc, str(exc), {"status_code": exc.status_code}
        )
    if isinstance(exc, AliasCircuitOpenError):
        # ResolvedModel contains authorization and backend overrides. Only
        # the identity and reason needed by the caller cross this boundary.
        return _provider_failure(
            exc,
            str(exc),
            {
                "alias": exc.alias,
                "model": exc.resolved.model,
                "reason": exc.reason,
            },
        )
    for error_type in (ValueError, RuntimeError):
        if isinstance(exc, error_type):
            return ModelFailure("domain", error_type.__name__, str(exc))
    # An unknown worker/programming failure must not become a fail-open judge.
    return ModelFailure("execution", "ExecutionFailure", name)
