"""Recorded provider failures preserve native policy and their billed usage."""

import json
from dataclasses import asdict

import httpx
import pytest
from gc_core.execution.contracts import (
    ExecutionFailure,
    InfrastructureFailure,
    ModelProviderError,
    ModelResult,
    OutcomeUnknown,
)
from gc_core.execution.model_errors import restore_model_failure
from gc_core.model_router import AliasCircuitOpenError, ResolvedModel
from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    is_auth_error,
    is_context_overflow_error,
    is_model_unavailable_error,
    is_previous_response_cache_miss,
    is_retriable_backend_error,
)
from gc_core.utils.chat_completions_stream import BackendStreamError
from gc_core_temporalio.model_error_encoding import encode_model_failure
from temporalio.converter import value_to_type


def _http_error(status, message="provider failed"):
    request = httpx.Request("POST", "https://private.test/?token=secret")
    return httpx.HTTPStatusError(
        message,
        request=request,
        response=httpx.Response(status, request=request),
    )


@pytest.mark.parametrize(
    "failure",
    [
        ValueError("invalid response"),
        RuntimeError("model unavailable"),
        _http_error(401),
        _http_error(503),
        _http_error(400, "maximum context length exceeded"),
        httpx.ReadTimeout("timeout"),
        httpx.ConnectError("connection lost"),
        httpx.LocalProtocolError("invalid header"),
        EmbeddedChoiceError("out of quota", raw_code="insufficient_quota"),
        EmbeddedChoiceError("provider failed", status_code=503, raw_code=503),
        BackendStreamError("failed", status_code=503),
        ExecutionFailure("worker failed"),
        InfrastructureFailure("database failed"),
        OutcomeUnknown("completion unknown"),
    ],
)
def test_wire_error_retains_native_consumer_classification(failure):
    result = ModelResult(failure=encode_model_failure(failure))
    wire = json.loads(json.dumps(asdict(result)))
    decoded = value_to_type(ModelResult, wire)
    restored = restore_model_failure(decoded.failure)
    if decoded.failure.kind == "provider":
        assert isinstance(restored, ModelProviderError)
        assert restored.failure.error_type == type(failure).__name__
    else:
        assert type(restored) is type(failure)
    for predicate in (
        is_auth_error,
        is_model_unavailable_error,
        is_context_overflow_error,
        is_retriable_backend_error,
    ):
        assert predicate(restored) == predicate(failure)
    assert "secret" not in json.dumps(wire)
    assert "private.test" not in json.dumps(wire)


def test_unknown_worker_error_is_not_restored_as_a_provider_error():
    class WorkerBug(Exception):
        pass

    restored = restore_model_failure(encode_model_failure(WorkerBug("boom")))
    assert isinstance(restored, ExecutionFailure)
    assert not is_model_unavailable_error(restored)


def test_alias_error_keeps_classification_without_resolved_credentials():
    resolved = ResolvedModel(
        alias="judge",
        model="judge-model",
        url="https://private.test/",
        authorization="Bearer secret",
        backend_overrides={"token": "secret"},
        tariff=None,
        source="test",
    )
    failure = encode_model_failure(AliasCircuitOpenError(resolved, "HTTP 503"))
    restored = restore_model_failure(failure)
    assert is_model_unavailable_error(restored)
    assert failure.details["alias"] == "judge"
    assert "secret" not in json.dumps(asdict(failure))
    assert "private.test" not in json.dumps(asdict(failure))


def test_old_successful_model_payload_still_decodes():
    payload = asdict(ModelResult(text="answer"))
    del payload["failure"]
    result = value_to_type(ModelResult, payload)
    assert result.text == "answer"
    assert result.failure is None


def test_responses_cache_miss_body_survives_without_provider_data():
    request = httpx.Request("POST", "https://private.test/?token=secret")
    response = httpx.Response(
        400,
        request=request,
        json={
            "error": {
                "message": "previous_response_id not found in cache; secret"
            }
        },
    )
    with pytest.raises(httpx.HTTPStatusError) as caught:
        response.raise_for_status()
    failure = encode_model_failure(caught.value)
    restored = restore_model_failure(failure)
    assert is_previous_response_cache_miss(caught.value)
    assert is_previous_response_cache_miss(restored)
    assert "secret" not in json.dumps(asdict(failure))
    assert "private.test" not in json.dumps(asdict(failure))
