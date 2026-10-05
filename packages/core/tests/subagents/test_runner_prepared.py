"""Prepared children use the real runner without discovering host resources."""

import asyncio
import builtins
import json
from functools import partial
from types import SimpleNamespace

import pytest
from gc_core.execution.contracts import OutcomeUnknown
from gc_core.state_preparation import PreparedState
from gc_core.states import State
from gc_core.tools.subagents import runner as runner_module
from gc_core.tools.subagents.active_scope import (
    activate_child_input,
    active_parent_call_id,
    try_enqueue_user_message,
)
from gc_core.tools.subagents.custom.file_definitions import SubagentDefinition
from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.tools.subagents.runner_preparation import (
    PreparedChildState,
    resolve_prepared_subagent,
)
from gc_core.usage.account import CATEGORY_MAIN
from gc_testkit.settings import make_settings


def _state():
    return State.from_prepared(
        PreparedState("parent", {}, "parent instructions"),
        verbose=False,
        model="model",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="prompt",
        use_server_subagents=False,
    )


class Preparation:
    def __init__(self):
        # Настройки ребёнка, подготовленные исполнителем заранее.
        self.config = make_settings({"agent.subagent_max_steps": 5})
        self.child_ctx = SimpleNamespace(
            run_id="child-run",
            logger=None,
            model_router=None,
            settings=self.config.settings,
            runtime=self.config.runtime,
            tools_data={},
            operations=object(),
        )
        self.released = []
        self.definitions = {
            "subagent_custom": SubagentDefinition(
                "subagent_custom",
                "custom",
                "Prepared role",
                None,
                (),
                "/worker-only/role.md",
            )
        }

    def resolve_subagent(self, tool_name):
        return resolve_prepared_subagent(tool_name, self.definitions)

    def build_child_config(self, _runner):
        return self.config

    async def build_child_context(self, _runner, _tool_name):
        return self.child_ctx

    async def prepare_child_state(self, _runner):
        return PreparedChildState(
            self.config,
            [],
            [],
            "Prepared template: {specific_role}",
            PreparedState(
                "child", {"currentDate": "pinned"}, "child instructions"
            ),
        )

    def release_child_context(self, ctx):
        self.released.append(ctx)


def _forbid(*_args, **_kwargs):
    raise AssertionError("Unexpected local preparation")


@pytest.mark.parametrize("tool_name", ["subagent_worker", "subagent_custom"])
async def test_prepared_runner_preserves_shared_state_usage_and_input(
    monkeypatch, tool_name
):
    parent = _state()
    preparation = Preparation()
    ctx = SimpleNamespace(
        tool_effect=object(),
        logger=None,
        subagent_preparation=preparation,
    )
    runner = SubagentRunner(ctx, parent, preparation.config)
    for name in (
        "get_subagent_class",
        "resolve_subagent_definitions",
        "abuild_tools_data",
        "get_skills_registry",
        "prepare_state",
        "create_run_logger",
        "release_run_logger",
    ):
        monkeypatch.setattr(runner_module, name, _forbid)
    monkeypatch.setattr(runner, "_resolve_subagent_system_prompt", _forbid)
    monkeypatch.setattr(asyncio, "run_coroutine_threadsafe", _forbid)
    monkeypatch.setattr(builtins, "open", _forbid)
    traces = []

    async def record(_ctx, *, kind, payload):
        await asyncio.sleep(0)
        traces.append((kind, payload))

    monkeypatch.setattr(runner_module, "apersist_subagent_trace", record)

    async def run_child(messages, child, child_ctx, **kwargs):
        assert child.state_id == "child"
        assert child.parent_state_id == "parent"
        assert child.shared is parent.shared
        assert child_ctx.parent_call_id == "parent-call"
        assert child_ctx.operations is preparation.child_ctx.operations
        assert active_parent_call_id(ctx) == "parent-call"
        assert messages[-1]["role"] == "user"
        assert try_enqueue_user_message(
            ctx, {"role": "user", "content": "extra", "id": "input-1"}
        )
        assert kwargs["get_pending_messages"]() == [
            {"role": "user", "content": "extra", "id": "input-1"}
        ]
        child.shared.working_memory["result"] = "shared"
        child.session_todo = [{"task": "child only", "status": "done"}]
        child.tool_calls = 2
        child.usage.add(CATEGORY_MAIN, input_tokens=3, output_tokens=2)
        kwargs["persist_messages_callback"](child.messages)
        child.tool_calls = 3
        return "done"

    output = json.loads(
        await runner.arun(
            tool_name,
            {"task": "work", "reason": "test"},
            run_child,
            parent_call_id="parent-call",
        )
    )
    assert output["ok"] is True, output
    assert output["meta"]["child_run_id"] == "child-run"
    assert parent.shared.working_memory["result"] == "shared"
    assert parent.session_todo == []
    assert parent.tool_calls == 3
    assert parent.usage.to_meta()["by_category"]["subagent"]["input"] == 3
    assert active_parent_call_id(ctx) is None
    assert [kind for kind, _ in traces] == ["user", "finished"]
    assert preparation.released == [preparation.child_ctx]


async def test_native_missing_preparation_cannot_fall_back_to_filesystem():
    runner = SubagentRunner(SimpleNamespace(tool_effect=object()), _state())
    with pytest.raises(RuntimeError, match="explicit preparation"):
        await runner.arun("subagent_worker", {"task": "work"}, _forbid)


async def test_unknown_child_effect_is_not_model_feedback():
    preparation = Preparation()
    ctx = SimpleNamespace(subagent_preparation=preparation)
    runner = SubagentRunner(
        ctx,
        _state(),
        preparation.config,
        input_scope_factory=partial(activate_child_input, thread_safe=False),
    )

    async def run_child(*_args, **_kwargs):
        raise OutcomeUnknown("child effect is uncertain")

    with pytest.raises(OutcomeUnknown):
        await runner.arun(
            "subagent_worker", {"task": "work", "reason": "test"}, run_child
        )
    assert preparation.released == [preparation.child_ctx]
