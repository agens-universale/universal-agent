"""Публичные ошибки сервера переживают настоящий кодек Temporal без потерь."""

from dataclasses import asdict

import httpx
import pytest
from gc_core.execution.contracts import ModelFailure
from gc_core.execution.model_errors import restore_model_failure
from gc_core.utils.chat_completions_stream import BackendStreamError
from gc_core_temporalio.model_error_encoding import encode_model_failure
from gc_server.chat.model_errors import model_api_error


@pytest.mark.parametrize("transport", ["sse", "http"])
@pytest.mark.parametrize(
    "code,status,public_code",
    [
        ("context_length_exceeded", 400, "context_length_exceeded"),
        ("rate_limit_exceeded", 503, "upstream_rate_limited"),
        ("unknown_provider_code", 502, "upstream_error"),
    ],
)
def test_known_provider_codes_survive_durable_encoding(
    transport, code, status, public_code
):
    if transport == "sse":
        error = BackendStreamError("private provider detail", status_code=code)
    else:
        response = httpx.Response(
            400,
            request=httpx.Request(
                "POST", "https://secret.invalid/model?token=private"
            ),
            json={
                "error": {"code": code, "message": "private provider detail"}
            },
        )
        with pytest.raises(httpx.HTTPStatusError) as raised:
            response.raise_for_status()
        error = raised.value
    failure = encode_model_failure(error)
    restored = restore_model_failure(failure)
    for candidate in [error, restored]:
        public = model_api_error(candidate)
        assert (public.status, public.code) == (status, public_code)
        assert (
            "private" not in public.message
            and "secret.invalid" not in public.message
        )
    assert failure.context_overflow == (code == "context_length_exceeded")
    assert set(failure.details) <= {"status_code", "provider_code"}
    if transport == "http":
        assert "private" not in str(asdict(failure))
        assert "secret.invalid" not in str(asdict(failure))


@pytest.mark.parametrize(
    "status,expected",
    [
        ("context_length_exceeded", "context_length_exceeded"),
        ("rate_limit_exceeded", "upstream_rate_limited"),
        ("401", "upstream_authentication_error"),
    ],
)
def test_existing_durable_histories_with_string_status_keep_classification(
    status, expected
):
    failure = ModelFailure(
        "provider",
        "BackendStreamError",
        "private",
        details={"status_code": status},
    )
    assert model_api_error(restore_model_failure(failure)).code == expected


@pytest.mark.parametrize(
    "body", [[], "private", {"error": []}, {"error": {"code": ["private"]}}]
)
def test_unrecognised_http_bodies_keep_generic_error_after_codec(body):
    response = httpx.Response(
        400,
        request=httpx.Request("POST", "https://example.invalid"),
        json=body,
    )
    with pytest.raises(httpx.HTTPStatusError) as raised:
        response.raise_for_status()
    failure = encode_model_failure(raised.value)
    assert failure.details == {"status_code": 400}
    assert (
        model_api_error(restore_model_failure(failure)).code
        == "upstream_error"
    )
