"""The native executor consumes prepared inputs, with local creation unchanged."""

from dataclasses import asdict
from types import SimpleNamespace

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.run_context import acreate_run_context
from gc_core.state_preparation import PreparedState


async def build_state(executor):
    return await executor._abuild_run_state(
        effective_verbose=False,
        skills_list=[],
        chat_completions_system_prompt=None,
        special_instructions=None,
        thread_id="thread-1",
        thread_shared=None,
        thread_session=None,
        backend_api_mode="chat_completions",
        logger=None,
    )


def context(preparer):
    return SimpleNamespace(
        config_dict={
            "model": "test-model",
            "max_steps": 10,
            "system_prompt_id": "test-prompt",
            "agent_md_path": "/worker/instructions.md",
            "chat_completion_headers": {
                "Authorization": "Bearer credential-not-to-be-sent"
            },
        },
        tools_spec=[],
        tools_description=[],
        state_preparer=preparer,
    )


async def test_native_executor_builds_real_state_from_prepared_inputs(
    monkeypatch,
):
    requests = []

    async def prepare(request):
        requests.append(asdict(request))
        return PreparedState(
            state_id="recorded-state-1",
            project_data={"currentDate": "2026-10-01"},
            instructions_text="Follow the project conventions.",
            instruction_warnings=["recorded warning"],
            long_memory_status="recorded memory status",
        )

    def forbidden(*_args, **_kwargs):
        raise AssertionError("executor must use its prepared input boundary")

    executor = AgentExecutor(context(prepare))
    monkeypatch.setattr("builtins.open", forbidden)
    monkeypatch.setattr("gc_core.states.uuid4", forbidden)
    state, rendered = await build_state(executor)

    assert state.state_id == "recorded-state-1"
    assert state.thread_id == "thread-1"
    assert state.instruction_warnings == ["recorded warning"]
    assert (
        state.initial_project_data["longMemoryStatus"]
        == "recorded memory status"
    )
    assert (
        "Follow the project conventions."
        in state.call_metadata["promptVariables"]["userInstructions"]
    )
    assert rendered is None
    assert requests == [
        {
            "agent_md_path": "/worker/instructions.md",
            "is_tui": False,
            "agentsmd_override": False,
            "verbose": False,
        }
    ]


async def test_state_preparation_failure_reaches_caller_without_local_fallback():
    failure = RuntimeError("preparation activity failed")

    async def prepare(_request):
        raise failure

    with pytest.raises(RuntimeError) as caught:
        await build_state(AgentExecutor(context(prepare)))

    assert caught.value is failure


async def test_sdk_passes_state_preparer_to_native_executor(
    tmp_path, monkeypatch
):
    monkeypatch.chdir(tmp_path)

    async def prepare(_request):
        return PreparedState("recorded-state", {}, "")

    cfg = {
        "save_logs": False,
        "backend_api_mode": "chat_completions",
        "model": "test-model",
        "system_prompt_id": "test-prompt",
        "max_steps": 10,
    }
    ctx = await acreate_run_context(cfg, state_preparer=prepare)
    state, _rendered = await build_state(AgentExecutor(ctx))

    assert state.state_id == "recorded-state"
