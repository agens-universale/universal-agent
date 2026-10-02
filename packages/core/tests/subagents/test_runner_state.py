"""Tests for SubagentRunner._build_child_state."""

from types import SimpleNamespace

import pytest
from gc_core.states import SharedStateData, State
from gc_core.tools.subagents.registry import is_subagent_tool
from gc_core.tools.subagents.runner import SubagentRunner


def _make_orchestrator_state(**overrides):
    shared = overrides.pop(
        "shared", SharedStateData(working_memory={"state": "test"})
    )
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}, {"name": "subagent_worker"}],
        tools_descr=["read desc", "worker desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="You are a helpful assistant.",
        specific_role=None,
        agent_md_path=None,
        shared=shared,
        **overrides,
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_config(**overrides):
    cfg = {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "subagent_max_steps": 30,
        "skills_data_path": "",
        "model": "test-model",
        "system_prompt_id": "test-prompt",
    }
    cfg.update(overrides)
    return cfg


def _make_ctx(config_dict=None):
    return SimpleNamespace(
        config_dict=config_dict or _make_config(),
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestBuildChildState:
    @pytest.mark.asyncio
    async def test_parent_state_id_equals_orchestrator(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.parent_state_id == orch.state_id

    @pytest.mark.asyncio
    async def test_shared_is_same_reference(self):
        shared = SharedStateData(working_memory={"state": "shared data"})
        orch = _make_orchestrator_state(shared=shared)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.shared is orch.shared
        assert child.shared is shared

    @pytest.mark.asyncio
    async def test_specific_role_set(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        role = "You are an expert planner."
        child = await runner._build_child_state(role)
        assert child.specific_role == role

    @pytest.mark.asyncio
    async def test_no_subagent_tools_in_child(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        child_tool_names = {
            t.get("name") for t in child.tools if isinstance(t, dict)
        }
        for name in child_tool_names:
            assert not is_subagent_tool(name), (
                f"Subagent tool {name} should NOT be in child state tools"
            )

    @pytest.mark.asyncio
    async def test_backend_api_mode_is_chat_completions(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.backend_api_mode == "chat_completions"

    @pytest.mark.asyncio
    async def test_max_steps_from_config(self):
        orch = _make_orchestrator_state()
        cfg = _make_config(subagent_max_steps=25)
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.max_steps == 25

    @pytest.mark.asyncio
    async def test_max_steps_default_when_not_in_config(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        del cfg["subagent_max_steps"]
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.max_steps == 50

    @pytest.mark.asyncio
    async def test_model_inherited_from_orchestrator(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.default_model == orch.default_model

    @pytest.mark.asyncio
    async def test_use_server_subagents_is_false(self):
        orch = _make_orchestrator_state()
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("test role")
        assert child.use_server_subagents is False
