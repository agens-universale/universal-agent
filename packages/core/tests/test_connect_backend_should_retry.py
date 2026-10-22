import httpx
from gc_core.utils.connect_backend import (
    _default_should_retry_backend_exception,
)


def _http_error(status: int) -> httpx.HTTPStatusError:
    """Двойник ошибки ТОГО транспорта, на котором ветка и работает.

    Тест приехал с develop, где ядро ходило через `requests`. У нас
    `HTTP_STATUS_ERRORS` — это `(httpx.HTTPStatusError,)`, и чужой тип не
    проходил `isinstance`: проверка проваливалась мимо всей классификации,
    а не из-за неё. Ожидания теста наш классификатор выполняет как есть.
    """
    request = httpx.Request("POST", "https://backend/v1/chat/completions")
    response = httpx.Response(status, request=request)
    return httpx.HTTPStatusError(
        f"HTTP {status}", request=request, response=response
    )


def test_transient_statuses_are_retried():
    # 404: the cluster LLM proxy answers 404 while a model route is missing after a
    # tunnel flap — transient, the route comes back within the retry budget.
    for code in (404, 429, 500, 503):
        assert (
            _default_should_retry_backend_exception(_http_error(code)) is True
        )


def test_client_errors_are_not_retried():
    for code in (400, 401, 403, 422):
        assert (
            _default_should_retry_backend_exception(_http_error(code)) is False
        )


def test_stream_auth_rejection_is_not_retried():
    from gc_core.utils.stream_assembler import BackendStreamError

    assert (
        _default_should_retry_backend_exception(
            BackendStreamError("Backend stream error", status_code=401)
        )
        is False
    )
