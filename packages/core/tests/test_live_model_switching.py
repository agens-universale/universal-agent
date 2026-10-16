"""A user level change affects the next request, never a prepared request."""

import asyncio
import json
from types import SimpleNamespace

import httpx
import pytest

from gc_core import processing_responses as pr
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules.root import AppConfig
from gc_core.execution.local import LocalOperations
from gc_core.run_context import acreate_run_context
from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_core.utils import connect_backend


async def _runtime(initial):
    selected = {"level": initial}
    cfg = AppConfig.model_validate(
        {
            "level": initial,
            "backend": {"api_mode": "chat_completions", "vision": False},
            "logging": {"save_logs": False},
            "agent": {"auto_routing_safe_level": "default"},
            "models": {
                alias: {
                    "model": alias,
                    "url": f"http://{alias}/v1/chat/completions",
                    "token": f"Bearer test-{alias}",
                    "summary_trigger_chars": threshold,
                }
                for alias, threshold in (
                    ("fast", 100000),
                    ("mid", 200000),
                    ("big", 300000),
                )
            },
            "slots": {"light": "fast", "default": "mid", "max": "big"},
        }
    )
    ctx = await acreate_run_context(
        cfg, level_provider=lambda: selected["level"]
    )
    state = State(
        verbose=False,
        model="m",
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.messages = [
        {"role": "system", "content": "Calculate sums.", "id": "sys"}
    ]
    executor = AgentExecutor(ctx)
    executor._bind_state(state, discard_writes_after_stop=False)
    begin_turn(state, ctx)
    ctx.operations = LocalOperations(ctx)
    return selected, ctx, state, executor


def _reply(index):
    return httpx.Response(
        200,
        json={
            "id": f"response-{index}",
            "object": "chat.completion",
            "choices": [
                {
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": f"call-{index}",
                                "type": "function",
                                "function": {
                                    "name": "exec_calculator",
                                    "arguments": json.dumps(
                                        {"expression": f"{index}+1"}
                                    ),
                                },
                            }
                        ],
                    },
                }
            ],
            "usage": {
                "prompt_tokens": 20,
                "completion_tokens": 10,
                "total_tokens": 30,
            },
        },
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "initial,target,models,thresholds,active",
    [
        (
            "light",
            "default",
            ["fast", "mid", "mid"],
            [100000, 200000, 200000],
            False,
        ),
        (
            "default",
            "light",
            ["mid", "fast", "fast"],
            [200000, 100000, 100000],
            False,
        ),
        (
            "light",
            "max",
            ["fast", "big", "big"],
            [100000, 300000, 300000],
            False,
        ),
        (
            "max",
            "light",
            ["big", "fast", "fast"],
            [300000, 100000, 100000],
            False,
        ),
        (
            "default",
            "max",
            ["mid", "big", "big"],
            [200000, 300000, 300000],
            False,
        ),
        (
            "max",
            "default",
            ["big", "mid", "mid"],
            [300000, 200000, 200000],
            False,
        ),
        (
            "light",
            "auto",
            ["fast", "mid", "mid"],
            [100000, 200000, 200000],
            True,
        ),
        (
            "default",
            "auto",
            ["mid", "mid", "mid"],
            [200000, 200000, 200000],
            True,
        ),
        ("max", "auto", ["big", "mid", "mid"], [300000, 200000, 200000], True),
        (
            "auto",
            "light",
            ["mid", "fast", "fast"],
            [200000, 100000, 100000],
            False,
        ),
        (
            "auto",
            "max",
            ["mid", "big", "big"],
            [200000, 300000, 300000],
            False,
        ),
        (
            "auto",
            "default",
            ["mid", "mid", "mid"],
            [200000, 200000, 200000],
            False,
        ),
    ],
)
async def test_live_selection_changes_the_next_payload_and_its_threshold(
    monkeypatch, initial, target, models, thresholds, active
):
    selected, ctx, state, executor = await _runtime(initial)
    sent = []
    limits = []

    def serve(request):
        payload = json.loads(request.content)
        sent.append(
            (
                payload["model"],
                request.url.host,
                request.headers["Authorization"],
            )
        )
        limits.append(executor.summary_limits().chars_during)
        if len(sent) == 1:
            selected["level"] = target
            # The first request still owns its original context threshold.
            assert executor.summary_limits().chars_during == thresholds[0]
        return _reply(len(sent))

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=transport),
    )
    await pr.arun_agent(
        [{"role": "user", "content": "Calculate three sums.", "id": "user"}],
        state,
        ctx,
        check_stop=lambda: len(sent) == 3,
    )
    assert [x[0] for x in sent] == models
    assert [x[1] for x in sent] == models
    assert [x[2] for x in sent] == [f"Bearer test-{model}" for model in models]
    assert limits == thresholds
    assert (state.current_level is not None) is active


@pytest.mark.asyncio
async def test_repeated_manual_and_auto_changes_apply_inside_one_user_turn(
    monkeypatch,
):
    selected, ctx, state, executor = await _runtime("light")
    changes = ["max", "default", "auto", "light", "auto", "max", "light"]
    models = []
    thresholds = []

    def serve(request):
        models.append(json.loads(request.content)["model"])
        thresholds.append(executor.summary_limits().chars_during)
        if len(models) <= len(changes):
            selected["level"] = changes[len(models) - 1]
        return _reply(len(models))

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=transport),
    )
    await pr.arun_agent(
        [{"role": "user", "content": "Calculate eight sums.", "id": "user"}],
        state,
        ctx,
        check_stop=lambda: len(models) == 8,
    )
    assert models == [
        "fast",
        "big",
        "mid",
        "mid",
        "fast",
        "mid",
        "big",
        "fast",
    ]
    assert thresholds == [
        100000,
        300000,
        200000,
        200000,
        100000,
        200000,
        300000,
        100000,
    ]


@pytest.mark.asyncio
async def test_change_while_preparing_keeps_the_request_then_applies_to_the_next_one(
    monkeypatch,
):
    selected, ctx, state, executor = await _runtime("light")
    sent = []
    seen_limits = []

    async def preparation(*args, **kwargs):
        if not sent:
            await asyncio.sleep(0)
            selected["level"] = "max"
            assert executor.summary_limits().chars_during == 100000

    def serve(request):
        sent.append(json.loads(request.content)["model"])
        seen_limits.append(executor.summary_limits().chars_during)
        return _reply(len(sent))

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=transport),
    )
    await pr.arun_agent(
        [{"role": "user", "content": "Calculate two sums.", "id": "user"}],
        state,
        ctx,
        pre_backend_compaction_hook=preparation,
        check_stop=lambda: len(sent) == 2,
    )
    assert sent == ["fast", "big"]
    assert seen_limits == [100000, 300000]


@pytest.mark.asyncio
async def test_auth_retry_keeps_the_call_kit_and_the_next_request_accepts_the_change(
    monkeypatch,
):
    selected, ctx, state, executor = await _runtime("light")
    sent = []
    thresholds = []

    async def token(*, force=False):
        return "test-refreshed"

    ctx.auth_connector = SimpleNamespace(aget_token=token)

    def serve(request):
        payload = json.loads(request.content)
        sent.append(
            (payload, request.url.host, request.headers["Authorization"])
        )
        thresholds.append(executor.summary_limits().chars_during)
        if len(sent) <= 2:
            if len(sent) == 1:
                selected["level"] = "max"
            return httpx.Response(401, json={"error": {"message": "expired"}})
        return _reply(len(sent))

    transport = httpx.MockTransport(serve)
    monkeypatch.setattr(
        connect_backend,
        "get_async_client",
        lambda **_: httpx.AsyncClient(transport=transport),
    )
    await pr.arun_agent(
        [{"role": "user", "content": "Calculate two sums.", "id": "user"}],
        state,
        ctx,
        check_stop=lambda: len(sent) == 4,
    )
    assert [x[0]["model"] for x in sent] == ["fast", "fast", "fast", "big"]
    assert sent[1][:2] == sent[0][:2]
    assert sent[2] == sent[0]
    # Existing transport negotiation can retry the same token without Bearer.
    assert sent[1][2] == "test-fast"
    assert thresholds == [100000, 100000, 100000, 300000]
