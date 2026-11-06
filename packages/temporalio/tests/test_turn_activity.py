"""Heartbeat и освобождение SDK при отмене целой activity."""

import asyncio

import pytest
from gc_core.settings import default_settings
from gc_core_temporalio import (
    AgentActivity,
    AgentActivityRequest,
    turn_activity,
)
from temporalio.exceptions import ApplicationError
from temporalio.testing import ActivityEnvironment


async def test_heartbeat_covers_bootstrap_and_cancellation_closes_agent(
    monkeypatch,
):
    entered, closed = asyncio.Event(), asyncio.Event()
    beats = []
    env = ActivityEnvironment()
    env.on_heartbeat = lambda *args: beats.append(args)

    class Agent:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *_args):
            closed.set()

        async def run(self, *_args, **_kwargs):
            entered.set()
            await asyncio.Event().wait()

    async def create(settings, **_kwargs):
        assert settings.runtime.human_channel is False
        assert settings.runtime.trace_id == "tr-1"
        await asyncio.sleep(0.05)
        assert len(beats) >= 2
        return Agent()

    monkeypatch.setattr(turn_activity.AsyncAgent, "create", create)
    runner = AgentActivity(
        {"a": default_settings()},
        thread_store=object(),
        heartbeat_interval=0.01,
    )
    task = asyncio.create_task(
        env.run(
            runner.run,
            AgentActivityRequest("a", "hello", "t", trace_id="tr-1"),
        )
    )
    await asyncio.wait_for(entered.wait(), 5)
    env.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert closed.is_set()
    count = len(beats)
    await asyncio.sleep(0.03)
    assert len(beats) == count


@pytest.mark.parametrize(
    "turn_request",
    [
        AgentActivityRequest("missing", "hello", "t"),
        AgentActivityRequest("a", "hello", ""),
        AgentActivityRequest("a", "hello", "t", trace_id="bad\ntrace"),
    ],
)
async def test_invalid_request_is_not_retried(turn_request):
    runner = AgentActivity({"a": default_settings()}, thread_store=object())
    with pytest.raises(ApplicationError) as error:
        await ActivityEnvironment().run(runner.run, turn_request)
    assert error.value.non_retryable
