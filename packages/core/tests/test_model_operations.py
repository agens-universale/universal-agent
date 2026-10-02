"""The native model callers retain provider behavior across the operation seam."""

import asyncio
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock

import httpx
import pytest
from gc_core import processing_responses as pr
from gc_core.config import flatten_app_config
from gc_core.config_modules.root import AppConfig
from gc_core.execution.contracts import ExecutionFailure, ModelResult
from gc_core.execution.finish_reason import is_context_overflow_error
from gc_core.execution.local import LocalOperations
from gc_core.model_router import ModelRouter
from gc_core.usage.account import UsageAccount
from gc_core.usage.context import record_usage
from gc_core.utils import llm_chat_client as llm
from gc_core.utils.backend_errors import EmbeddedChoiceError


def _state():
    usage = UsageAccount()
    usage.add("main", 13, 2)
    return SimpleNamespace(
        state_id="state-1",
        thread_id="thread-1",
        parent_state_id=None,
        call_id=None,
        x_target=None,
        default_model="test-model",
        usage=usage,
        messages=[{"role": "user", "content": "question"}],
        tools=[],
        call_metadata={},
        previous_response_id="previous-1",
        turn_level="default",
        last_main_prompt_tokens=13,
        last_main_prompt_message_count=1,
        last_finish_reason="stop",
    )


def _ctx(with_port, **config):
    ctx = SimpleNamespace(
        config_dict={
            "completions_url": "https://model.test/v1/chat/completions",
            "backend_request_timeout_sec": 600,
            "backend_api_mode": "chat_completions",
            **config,
        },
        auth_connector=None,
        logger=None,
    )
    ctx.operations = LocalOperations(ctx) if with_port else None
    return ctx


async def _call(consumer, ctx, state):
    if consumer == "main":
        return await pr._arequest_backend_canonical(state.messages, state, ctx)
    return await llm.arequest_chat_completion_outcome(
        ctx=ctx,
        state=state,
        messages=state.messages,
        max_tokens=42,
        consumer=consumer,
        request_label=consumer,
    )


@pytest.mark.parametrize("with_port", [False, True])
@pytest.mark.parametrize("finish_reason", ["length", "stop", None])
async def test_aux_outcome_retains_finish_reason_and_usage(
    monkeypatch, with_port, finish_reason
):
    captured = []

    async def policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured.append((payload, kwargs))
        record_usage({"prompt_tokens": 11, "completion_tokens": 7})
        return {
            "choices": [
                {
                    "message": {"content": "  summary result  "},
                    "finish_reason": finish_reason,
                }
            ],
        }, payload

    monkeypatch.setattr(llm, "arequest_chat_completions_with_policy", policy)
    state = _state()
    ctx = _ctx(with_port, chat_completion_kwargs={"temperature": 0.1})
    outcome = await llm.arequest_chat_completion_outcome(
        ctx=ctx,
        state=state,
        messages=state.messages,
        max_tokens=42,
        consumer="summary",
        request_label="summary_batch_1",
        payload_overrides={"temperature": 0.2},
    )

    assert outcome == llm.ChatCompletionText("summary result", finish_reason)
    assert state.usage.totals()["calls"] == 2
    assert state.usage.by_category["summary"]["input"] == 11
    assert state.usage.by_category["summary"]["output"] == 7
    assert state.last_finish_reason == "stop"
    assert state.last_main_prompt_tokens == 13
    assert state.last_main_prompt_message_count == 1
    assert len(captured) == 1
    payload, kwargs = captured[0]
    assert payload["temperature"] == 0.2
    assert payload["max_completion_tokens"] == 42
    assert "tools" not in payload
    assert kwargs["request_label"] == "summary_batch_1"
    assert kwargs["salvage_reasoning"] is False


@pytest.mark.parametrize("with_port", [False, True])
async def test_text_wrapper_uses_the_same_outcome_path(monkeypatch, with_port):
    policy = AsyncMock(
        return_value=(
            {"choices": [{"message": {"content": " result "}}]},
            {},
        )
    )
    monkeypatch.setattr(llm, "arequest_chat_completions_with_policy", policy)
    state = _state()
    result = await llm.arequest_chat_completion_text(
        ctx=_ctx(with_port),
        state=state,
        messages=state.messages,
        max_tokens=42,
        consumer="summary",
    )
    assert result == "result"
    assert policy.await_count == 1


@pytest.mark.parametrize(
    "consumer",
    [
        "summary",
        "finish_gate",
        "text_answer_judge",
        "intent_scope",
        "command_judge",
        "memory_compaction",
    ],
)
async def test_aux_outcome_cannot_bypass_the_configured_operation(
    monkeypatch, consumer
):
    direct = AsyncMock(side_effect=AssertionError("provider bypassed port"))
    monkeypatch.setattr(llm, "_arequest_chat_completion_outcome_local", direct)
    usage = UsageAccount()
    usage.add(consumer, 5, 3)
    operation = AsyncMock(
        return_value=ModelResult(
            text="complete", finish_reason="length", usage=usage.to_meta()
        )
    )
    ctx = SimpleNamespace(
        operations=SimpleNamespace(
            next_operation_id=lambda kind: f"{kind}-123", model=operation
        )
    )
    state = _state()
    result = await _call(consumer, ctx, state)

    assert result == llm.ChatCompletionText("complete", "length")
    assert operation.await_count == 1
    request = operation.await_args.args[0]
    assert request.consumer == consumer
    assert request.operation_id == "model-123"
    assert request.messages == state.messages
    assert request.messages is not state.messages
    assert state.usage.by_category[consumer]["calls"] == 1
    assert state.last_finish_reason == "stop"
    direct.assert_not_awaited()


@pytest.mark.parametrize("with_port", [False, True])
async def test_main_retains_reasoning_tool_arguments_finish_and_usage(
    monkeypatch, with_port
):
    captured = []
    argument_text = '{"path":"unfinished'
    reasoning = "Reasoning as returned by the provider. " * 200

    async def policy(*, payload_factory, initial_max_tokens, **kwargs):
        payload = payload_factory(initial_max_tokens)
        captured.append(kwargs)
        usage = {"prompt_tokens": 31, "completion_tokens": 19}
        record_usage(usage)
        return {
            "id": "chat-1",
            "choices": [
                {
                    "message": {
                        "role": "assistant",
                        "content": "Checking the file.",
                        "reasoning_content": reasoning,
                        "tool_calls": [
                            {
                                "id": "call-1",
                                "type": "function",
                                "function": {
                                    "name": "fs_read_file",
                                    "arguments": argument_text,
                                },
                            }
                        ],
                    },
                    "finish_reason": "length",
                }
            ],
            "usage": usage,
        }, payload

    monkeypatch.setattr(pr, "arequest_chat_completions_with_policy", policy)
    state = _state()
    ctx = _ctx(with_port, strip_tags_projection={"strip_think": False})
    messages, response_id, metadata = await _call("main", ctx, state)

    assert response_id == "chat-1"
    assert metadata == {}
    assert messages[0]["reasoning_content"] == reasoning.strip()
    tool = next(item for item in messages if item["type"] == "function_call")
    assert tool["name"] == "fs_read_file"
    assert tool["call_id"] == "call-1"
    assert tool["arguments"] == argument_text
    assert state.last_finish_reason == "length"
    assert state.last_main_prompt_tokens == 31
    assert state.last_main_prompt_message_count == 1
    assert state.usage.totals()["calls"] == 2
    assert state.usage.totals()["input"] == 44
    assert state.usage.totals()["output"] == 21
    assert len(captured) == 1
    assert captured[0]["salvage_reasoning"] is True


@pytest.mark.parametrize("with_port", [False, True])
async def test_responses_items_and_agent_metadata_survive_the_boundary(
    monkeypatch, with_port
):
    output = [
        {"id": "reasoning-1", "type": "reasoning", "summary": []},
        {
            "id": "message-1",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "output_text", "text": "answer"}],
        },
    ]
    headers = {"x-target": "worker-7", "set-cookie": "private=secret"}
    payloads = []

    async def backend(payload, _headers, **_kwargs):
        payloads.append(payload)
        record_usage({"input_tokens": 5, "output_tokens": 4})
        return {"output": deepcopy(output), "id": "response-7"}, headers

    monkeypatch.setattr(pr, "arequest_backend", backend)
    state = _state()
    result = await pr._arequest_backend_canonical(
        state.messages,
        state,
        _ctx(with_port, backend_api_mode="responses"),
        set_previous_response_id=False,
    )
    messages, response_id, metadata = result
    assert messages == output
    assert response_id == "response-7"
    assert metadata["x-target"] == "worker-7"
    if with_port:
        # Only metadata consumed by State may enter durable model results.
        assert metadata == {"x-target": "worker-7"}
    else:
        assert metadata == headers
    assert payloads[0]["previous_response_id"] is None
    assert state.last_finish_reason == "stop"
    assert state.last_main_prompt_tokens == 13
    assert state.usage.totals()["input"] == 18
    assert state.usage.totals()["output"] == 6


@pytest.mark.parametrize("with_port", [False, True])
async def test_aux_router_fallback_stays_in_the_native_provider_handler(
    monkeypatch, with_port
):
    config = flatten_app_config(
        AppConfig.model_validate(
            {
                "models": {"judge": {}, "active": {}},
                "slots": {"default": "active"},
                "assign": {"finish_gate": "judge"},
            }
        )
    )
    router = ModelRouter(config, level_provider=lambda: "default")
    models = []

    async def policy(*, payload_factory, initial_max_tokens, **_kwargs):
        payload = payload_factory(initial_max_tokens)
        models.append(payload["model"])
        if payload["model"] == "judge":
            request = httpx.Request("POST", "https://model.test")
            raise httpx.HTTPStatusError(
                "unavailable",
                request=request,
                response=httpx.Response(503, request=request),
            )
        record_usage({"prompt_tokens": 5, "completion_tokens": 2})
        return {
            "choices": [
                {"message": {"content": "approved"}, "finish_reason": "stop"}
            ]
        }, payload

    monkeypatch.setattr(llm, "arequest_chat_completions_with_policy", policy)
    ctx = _ctx(with_port)
    ctx.model_router = router
    state = _state()
    for _ in range(2):
        outcome = await _call("finish_gate", ctx, state)
        assert outcome == llm.ChatCompletionText("approved", "stop")
    assert models == ["judge", "active", "active"]
    assert router.breaker.is_open("judge")
    assert not router.breaker.is_open("active")
    assert state.usage.by_category["finish_gate"]["calls"] == 2


@pytest.mark.parametrize("with_port", [False, True])
@pytest.mark.parametrize("consumer", ["main", "summary"])
async def test_provider_exception_keeps_native_error_semantics(
    monkeypatch, with_port, consumer
):
    failure = ValueError("backend returned an invalid response")
    policy = AsyncMock(side_effect=failure)
    module = pr if consumer == "main" else llm
    monkeypatch.setattr(
        module, "arequest_chat_completions_with_policy", policy
    )
    state = _state()
    with pytest.raises(ValueError) as caught:
        await _call(consumer, _ctx(with_port), state)
    assert caught.value is failure
    assert policy.await_count == 1


@pytest.mark.parametrize("consumer", ["main", "summary"])
async def test_classified_context_overflow_reaches_native_error_policy(
    consumer,
):
    usage = UsageAccount()
    usage.add(consumer, 3, 1)
    operation = AsyncMock(
        return_value=ModelResult(context_overflow=True, usage=usage.to_meta())
    )
    ctx = SimpleNamespace(
        operations=SimpleNamespace(
            next_operation_id=lambda kind: f"{kind}-1", model=operation
        )
    )
    state = _state()
    with pytest.raises(RuntimeError) as caught:
        await _call(consumer, ctx, state)
    assert is_context_overflow_error(caught.value)
    assert operation.await_count == 1
    assert state.last_main_prompt_tokens == 13
    assert state.last_main_prompt_message_count == 1
    assert state.last_finish_reason == "stop"
    assert state.usage.totals()["calls"] == 2


@pytest.mark.parametrize("with_port", [False, True])
@pytest.mark.parametrize(
    "request_fn",
    [llm.arequest_chat_completion_text, llm.arequest_chat_completion_outcome],
)
async def test_invalid_token_limit_is_rejected_before_dispatch(
    with_port, request_fn
):
    ctx = _ctx(with_port)
    operation = AsyncMock(side_effect=AssertionError("must not dispatch"))
    if with_port:
        ctx.operations.model = operation
    state = _state()
    with pytest.raises(ValueError, match="max_tokens must be >= 1"):
        await request_fn(
            ctx=ctx,
            state=state,
            messages=state.messages,
            max_tokens=0,
            consumer="summary",
        )
    operation.assert_not_awaited()


@pytest.mark.parametrize("consumer", ["main", "summary"])
async def test_billed_retry_exhaustion_keeps_usage_with_and_without_port(
    monkeypatch, consumer
):
    from gc_core import request_backend_policy as policy

    async def transport(*_args, **_kwargs):
        record_usage({"prompt_tokens": 10, "completion_tokens": 3})
        return {"choices": [{"error": {"code": 503, "message": "failed"}}]}

    monkeypatch.setattr(policy, "arequest_backend_chat_completions", transport)
    monkeypatch.setattr(policy.asyncio, "sleep", AsyncMock())
    accounts = []
    for with_port in (False, True):
        state = _state()
        with pytest.raises(EmbeddedChoiceError) as caught:
            await _call(consumer, _ctx(with_port), state)
        assert caught.value.status_code == 503
        accounts.append(state.usage.to_meta())
        calls = 1 + policy._CHAT_COMPLETIONS_ADAPTIVE_MAX_TOKEN_STEPS
        assert state.usage.totals()["calls"] == 1 + calls
        assert state.usage.totals()["input"] == 13 + 10 * calls
        assert state.usage.totals()["output"] == 2 + 3 * calls
    assert accounts[0] == accounts[1]


@pytest.mark.parametrize("with_port", [False, True])
@pytest.mark.parametrize("consumer", ["main", "summary"])
@pytest.mark.parametrize("kind", ["domain", "overflow", "execution", "cancel"])
async def test_partial_usage_and_original_error_survive_failure(
    monkeypatch, with_port, consumer, kind
):
    errors = {
        "domain": ValueError("invalid backend response"),
        "overflow": ValueError("maximum context length exceeded"),
        "execution": ExecutionFailure("worker unavailable"),
        "cancel": asyncio.CancelledError("stopped by caller"),
    }
    failure = errors[kind]

    async def policy(**_kwargs):
        record_usage({"prompt_tokens": 17, "completion_tokens": 5})
        raise failure

    module = pr if consumer == "main" else llm
    monkeypatch.setattr(
        module, "arequest_chat_completions_with_policy", policy
    )
    state = _state()
    with pytest.raises(type(failure)) as caught:
        await _call(consumer, _ctx(with_port), state)
    assert caught.value is failure
    assert state.usage.totals()["calls"] == 2
    assert state.usage.totals()["input"] == 30
    assert state.usage.totals()["output"] == 7
    assert is_context_overflow_error(caught.value) == (kind == "overflow")
    if kind == "cancel":
        assert not isinstance(caught.value, Exception)


@pytest.mark.parametrize("with_port", [False, True])
@pytest.mark.parametrize("kind", ["domain", "execution", "cancel"])
async def test_native_finish_gate_only_fails_open_on_provider_errors(
    monkeypatch, with_port, kind
):
    from gc_core.intent_guard import guard as guard_module

    failures = {
        "domain": ValueError("invalid judge response"),
        "execution": ExecutionFailure("model activity failed"),
        "cancel": asyncio.CancelledError("stop"),
    }
    failure = failures[kind]

    async def policy(**_kwargs):
        record_usage({"prompt_tokens": 17, "completion_tokens": 5})
        raise failure

    async def validate(**kwargs):
        return await _call("finish_gate", kwargs["ctx"], kwargs["state"])

    monkeypatch.setattr(llm, "arequest_chat_completions_with_policy", policy)
    monkeypatch.setattr(
        guard_module, "avalidate_final_report_vs_user_intent", validate
    )
    state = _state()
    state.current_user_intent = ["finish the task"]
    state.user_intent_log = ["finish the task"]
    state.finish_gate_rejection_count = 0
    guard = guard_module.FinishGateGuard(
        ctx=_ctx(with_port, strict_turn_finish_validation=True), state=state
    )
    if kind == "domain":
        decision = await guard.aevaluate_turn_finish(
            finish_handoff_arguments={}
        )
        assert decision.outcome == "fail_open_error"
    else:
        with pytest.raises(type(failure)) as caught:
            await guard.aevaluate_turn_finish(finish_handoff_arguments={})
        assert caught.value is failure
    assert state.finish_gate_rejection_count == 0
    assert state.usage.by_category["finish_gate"]["input"] == 17
