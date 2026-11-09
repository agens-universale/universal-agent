import asyncio
import json
from dataclasses import asdict
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from gc_core import processing_responses as pr
from gc_core.execution.contracts import (
    ExecutionFailure,
    ModelResult,
    StateDelta,
)
from gc_core.execution.environment import execution_environment
from gc_core.execution.reducers import apply_state_delta
from gc_core.execution.state import ExecutionState
from gc_core.states import State
from gc_core.tools import aapply_tool, aapply_tool_result
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.utils.generate_message_id import generate_msg_id_sha256
from gc_core.utils.llm_chat_client import arequest_chat_completion_text
from gc_testkit.settings import fake_ctx, make_settings


def state():
    return State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )


@pytest.mark.parametrize(
    "name,args,field",
    [
        (
            "session_memory",
            {"action": "append", "register": "knowledge", "content": "fact"},
            "working_memory",
        ),
        (
            "session_todo",
            {"todos": [{"content": "test", "status": "pending"}]},
            "session_todo",
        ),
    ],
)
async def test_result_does_not_mutate_live_state_and_legacy_api_matches(
    name, args, field
):
    before = state()
    settings = make_settings()
    ctx = fake_ctx(
        loaded=settings, tools_data=get_builtin_tools_data(settings)
    )
    original = before.to_execution_snapshot()
    result = await aapply_tool_result(name, args, ctx=ctx, state=before)
    assert before.to_execution_snapshot() == original
    assert getattr(result.delta, field)
    assert json.loads(json.dumps(asdict(result)))["schema_version"] == 1
    apply_state_delta(before, result.delta)
    legacy = state()
    assert (
        await aapply_tool(name, args, ctx=ctx, state=legacy) == result.output
    )
    assert before.shared == legacy.shared
    # Reapplying replacement data is safe; append was computed only once.
    apply_state_delta(before, result.delta)
    assert before.shared == legacy.shared


def test_todo_delta_keeps_child_scope():
    parent = state()
    parent.shared.session_todo = [{"content": "parent", "status": "pending"}]
    child = state()
    child.parent_state_id = parent.state_id
    child.shared = parent.shared
    apply_state_delta(child, StateDelta(session_todo=[]))
    assert parent.session_todo[0]["content"] == "parent"
    assert child.session_todo == []


def test_snapshot_preserves_replay_data_and_excludes_resources():
    value = state()
    value.logger = object()
    value.workspace = object()
    value.on_messages_evicted = lambda _: None
    value.loop_detector_window = ["fingerprint"]
    value.loop_detector_pending = [{"name": "repeat"}]
    value.shared.confirm_rules = {"deny": False}
    value.last_main_prompt_tokens = 47
    value.last_main_prompt_message_count = 3
    value.usage.add("main", 47, 8)
    encoded = json.loads(json.dumps(value.to_execution_snapshot()))
    restored = ExecutionState.from_execution_snapshot(encoded)
    assert restored.to_execution_snapshot() == encoded
    assert restored.loop_detector_window == ["fingerprint"]
    assert restored.usage.totals()["total"] == 55
    assert "workspace" not in encoded["data"]
    assert "logger" not in encoded["data"]


def test_snapshot_roundtrip_keeps_applied_choice_and_reads_old_snapshot():
    value = state()
    value.applied_choice = "max"
    encoded = json.loads(json.dumps(value.to_execution_snapshot()))
    restored = ExecutionState.from_execution_snapshot(encoded)
    assert restored.applied_choice == "max"
    del encoded["data"]["applied_choice"]
    old = ExecutionState.from_execution_snapshot(encoded)
    assert old.applied_choice is None


async def test_deterministic_ids_are_task_local():
    async def run(label):
        with execution_environment(
            new_id=lambda prefix: f"{label}-{prefix}",
            now=lambda: datetime(2026, 1, 1, tzinfo=UTC),
        ):
            await asyncio.sleep(0)
            return generate_msg_id_sha256("m")

    assert await asyncio.gather(run("a"), run("b")) == ["a-m", "b-m"]


@pytest.mark.parametrize(
    "consumer",
    [
        "summary",
        "finish_gate",
        "text_answer_judge",
        "intent_scope",
        "command_judge",
    ],
)
async def test_every_aux_consumer_uses_the_port_and_merges_usage(consumer):
    value = state()
    calls = []

    class Operations:
        def next_operation_id(self, kind):
            return f"{kind}-1"

        async def model(self, request):
            calls.append(request)
            usage = state().usage
            usage.add(consumer, 5, 3)
            return ModelResult(text="done", usage=usage.to_meta())

    ctx = SimpleNamespace(operations=Operations())
    value.last_main_prompt_tokens = 19
    assert (
        await arequest_chat_completion_text(
            ctx=ctx,
            state=value,
            messages=[{"role": "user", "content": "input"}],
            max_tokens=42,
            consumer=consumer,
        )
        == "done"
    )
    assert calls[0].consumer == consumer
    assert value.usage.totals()["total"] == 8
    assert value.last_main_prompt_tokens == 19


async def test_persistence_failure_is_not_swallowed_or_bounced():
    value = state()

    async def persist(_):
        raise ExecutionFailure("database unavailable")

    with pytest.raises(ExecutionFailure):
        await pr.arun_agent(
            [{"id": "u", "role": "user", "content": "hi"}],
            value,
            fake_ctx({"backend.api_mode": "chat_completions"}),
            persist_messages_callback=persist,
        )
