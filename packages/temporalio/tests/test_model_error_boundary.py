"""Recorded provider failures preserve native policy and their billed usage."""

import asyncio
import json
from dataclasses import asdict
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from gc_core.execution.contracts import (
    ExecutionFailure,
    InfrastructureFailure,
    ModelProviderError,
    ModelRequest,
    ModelResult,
    OutcomeUnknown,
)
from gc_core.execution.local import LocalOperations
from gc_core.execution.model import arequest_model
from gc_core.execution.model_errors import restore_model_failure
from gc_core.model_router import AliasCircuitOpenError, ResolvedModel
from gc_core.usage.account import UsageAccount
from gc_core.usage.context import record_usage
from gc_core.utils import llm_chat_client as llm
from gc_core.utils.backend_errors import (
    EmbeddedChoiceError,
    is_auth_error,
    is_context_overflow_error,
    is_model_unavailable_error,
    is_previous_response_cache_miss,
    is_retriable_backend_error,
)
from gc_core.utils.chat_completions_stream import BackendStreamError
from gc_ua_temporalio import operations as ops_module
from gc_ua_temporalio.activities import AgentActivities
from gc_ua_temporalio.model_error_encoding import encode_model_failure
from gc_ua_temporalio.operations import TemporalOperations
from gc_ua_temporalio.storage.sqlite import SQLiteThreadStore
from temporalio.converter import value_to_type
from temporalio.exceptions import ActivityError, CancelledError, RetryState


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


def _state():
    return SimpleNamespace(
        usage=UsageAccount(),
        default_model="model",
        messages=[],
        tools=[],
        call_metadata={},
        state_id="state-1",
        x_target=None,
    )


def _start():
    return SimpleNamespace(
        profile="profile",
        profile_revision="revision",
        model_queue="models",
    )


@pytest.mark.parametrize(
    "kind", ["domain", "overflow", "execution", "unknown"]
)
async def test_recorded_failure_merges_usage_then_restores_error(
    monkeypatch, tmp_path, kind
):
    failures = {
        "domain": ValueError("invalid provider response"),
        "overflow": ValueError("maximum context length exceeded"),
        "execution": InfrastructureFailure("model worker lost resource"),
        "unknown": LookupError("unexpected bug"),
    }
    calls = []

    async def policy(**_kwargs):
        calls.append(True)
        record_usage({"prompt_tokens": 19, "completion_tokens": 7})
        raise failures[kind]

    monkeypatch.setattr(llm, "arequest_chat_completions_with_policy", policy)
    ctx = SimpleNamespace(
        config_dict={
            "completions_url": "https://model.test/",
            "backend_request_timeout_sec": 600,
        },
        logger=None,
    )
    local = LocalOperations(ctx)
    runtime = SimpleNamespace(
        model=lambda data: local.model(data.request),
        store=SQLiteThreadStore(str(tmp_path / "worker.sqlite")),
        config=SimpleNamespace(artifacts=str(tmp_path / "artifacts")),
    )
    activities = AgentActivities(runtime)
    commands = []

    async def execute(name, data, result_type, **_kwargs):
        assert name == "ua.model"
        commands.append(data)
        result = await activities._recorded_attempt(
            data, data.request.operation_id, result_type, activities._model
        )
        return value_to_type(
            result_type, json.loads(json.dumps(asdict(result)))
        )

    monkeypatch.setattr(ops_module, "execute", execute)
    state = _state()
    expected_type = (
        ExecutionFailure if kind == "unknown" else type(failures[kind])
    )
    with pytest.raises(expected_type) as caught:
        await arequest_model(
            TemporalOperations(_start(), "owner"),
            state,
            [],
            consumer="summary",
            max_tokens=42,
            request_label="summary",
        )
    assert is_context_overflow_error(caught.value) == (kind == "overflow")
    assert state.usage.by_category["summary"]["input"] == 19
    assert state.usage.by_category["summary"]["output"] == 7
    assert state.usage.totals()["calls"] == 1
    # Delivery repeated after a worker retry reads the same failed outcome;
    # it must not call the already exhausted native provider again.
    repeated = await execute("ua.model", commands[0], ModelResult)
    assert repeated.failure is not None
    assert repeated.usage == state.usage.to_meta()
    assert len(calls) == 1


async def test_model_cancellation_is_never_encoded_as_domain_failure(tmp_path):
    cancellation = asyncio.CancelledError("stop")
    usage = UsageAccount()
    usage.add("summary", 5, 2)
    from gc_core.execution.contracts import ModelOperationCancelled

    runtime = SimpleNamespace(
        model=AsyncMock(
            side_effect=ModelOperationCancelled(
                cancellation, ModelResult(usage=usage.to_meta())
            )
        ),
        store=SQLiteThreadStore(str(tmp_path / "worker.sqlite")),
        config=SimpleNamespace(artifacts=str(tmp_path / "artifacts")),
    )
    activities = AgentActivities(runtime)
    with pytest.raises(asyncio.CancelledError) as caught:
        await activities._model(None)
    assert caught.value is cancellation
    assert not isinstance(caught.value, Exception)


@pytest.mark.parametrize("cancelled", [False, True])
async def test_activity_failure_and_cancellation_stay_distinct(
    monkeypatch, cancelled
):
    activity_error = ActivityError(
        "activity failed",
        scheduled_event_id=1,
        started_event_id=2,
        identity="worker",
        activity_type="ua.model",
        activity_id="model-1",
        retry_state=RetryState.CANCEL_REQUESTED
        if cancelled
        else RetryState.TIMEOUT,
    )
    if cancelled:
        activity_error.__cause__ = CancelledError("cancelled")

    async def execute(*_args, **_kwargs):
        raise ExecutionFailure("Activity ua.model failed") from activity_error

    monkeypatch.setattr(ops_module, "execute", execute)
    operations = TemporalOperations(_start(), "owner")
    expected_type = asyncio.CancelledError if cancelled else ExecutionFailure
    with pytest.raises(expected_type):
        await operations.model(ModelRequest("model-1", "summary", [], {}))
