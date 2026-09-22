"""Tests for SubagentRunner._build_initial_messages."""
import pytest

from types import SimpleNamespace

from gc_core.intent_log.user_turn import init_subagent_open_turn
from gc_core.states import State, SharedStateData
from gc_core.tools.subagents.runner import SubagentRunner


def _make_orchestrator_state(system_prompt=None):
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt=system_prompt,
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    return state


def _make_config(**overrides):
    cfg = {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
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


def _user_msg(content: str) -> dict:
    return {"role": "user", "content": content, "id": "m_user_sub"}


class TestBuildInitialMessages:
    @pytest.mark.asyncio
    async def test_with_system_prompt_has_system_and_user(self):
        prompt = "You are a helpful assistant.\n<specific_role>default</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("Expert planner")
        messages = runner._build_initial_messages(
            child, "Expert planner", _user_msg("Do the task")
        )

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_system_prompt_contains_agent_role(self):
        prompt = "You are a helpful assistant.\n<specific_role>default</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("Expert planner")
        messages = runner._build_initial_messages(
            child, "Expert planner", _user_msg("Do the task")
        )

        assert "Expert planner" in messages[0]["content"]

    @pytest.mark.asyncio
    async def test_user_message_contains_task(self):
        prompt = "System prompt\n<specific_role>x</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        messages = runner._build_initial_messages(
            child, "role", _user_msg("Implement feature X")
        )

        user_msg = [m for m in messages if m["role"] == "user"]
        assert len(user_msg) == 1
        assert user_msg[0]["content"] == "Implement feature X"

    @pytest.mark.asyncio
    async def test_without_system_prompt_starts_with_user(self):
        orch = _make_orchestrator_state(system_prompt=None)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        messages = runner._build_initial_messages(child, "role", _user_msg("Do the task"))

        # При отсутствии промпта в State подставляется дефолтный файл субагента.
        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"
        assert messages[1]["content"] == "Do the task"

    @pytest.mark.asyncio
    async def test_empty_system_prompt_starts_with_user(self):
        orch = _make_orchestrator_state(system_prompt="   ")
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        messages = runner._build_initial_messages(child, "role", _user_msg("task text"))

        assert len(messages) == 2
        assert messages[0]["role"] == "system"
        assert messages[1]["role"] == "user"

    @pytest.mark.asyncio
    async def test_messages_have_id_field(self):
        prompt = "System\n<specific_role>x</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        messages = runner._build_initial_messages(child, "role", _user_msg("task"))

        for msg in messages:
            assert "id" in msg and isinstance(msg["id"], str) and len(msg["id"]) > 0

    @pytest.mark.asyncio
    async def test_child_state_messages_updated_with_system(self):
        prompt = "System prompt\n<specific_role>x</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        messages = runner._build_initial_messages(child, "role", _user_msg("task"))

        assert len(child.messages) >= 1
        assert child.messages[0]["role"] == "system"

    @pytest.mark.asyncio
    async def test_run_seeds_assignment_into_current_user_intent(self):
        """Как в SubagentRunner.run: transcript и finish-gate scope раздельно."""
        prompt = "System prompt\n<specific_role>x</specific_role>"
        orch = _make_orchestrator_state(system_prompt=prompt)
        cfg = _make_config()
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        child = await runner._build_child_state("role")
        user_message = _user_msg("Implement feature X")
        runner._build_initial_messages(child, "role", user_message)
        init_subagent_open_turn(child, "Implement feature X")

        from gc_core.intent_log import ACTION_USER_ASK

        assert len(child.current_user_intent) == 1
        assert child.current_user_intent[0].action == ACTION_USER_ASK
        assert child.current_user_intent[0].content == "Implement feature X"
        assert child.user_intent_log == []
        assert child.finish_gate_rejection_count == 0
