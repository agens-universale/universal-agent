"""Tests for summary integration inside SubagentRunner."""

from types import SimpleNamespace
from unittest.mock import patch

from states import State, SharedStateData, save_state
from tools.subagents import runner as runner_module
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}],
        tools_descr=["read desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="System\n<specific_role>x</specific_role>",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    save_state(state)
    return state


def _make_config(trigger=100):
    return {
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
        "summary_params": {
            "chat_completions_summary_trigger_chars": trigger,
        },
    }


def _make_ctx(cfg):
    return SimpleNamespace(
        config_dict=cfg,
        auth_connector=None,
        logger=None,
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )


class TestRunnerSummary:
    def test_passes_summary_hooks_to_run_agent(self):
        orch = _make_orchestrator_state()
        cfg = _make_config(trigger=10)
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        captured = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured.update(kwargs)
            return "ok"

        with (
            patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()),
            patch.object(runner_module, "maybe_run_summary_compaction", return_value=False),
        ):
            runner.run("subagent_worker", {"task": "short task"}, fake_run_agent)

        assert callable(captured.get("persist_messages_callback"))
        assert callable(captured.get("post_step_hook"))

    def test_runs_only_post_step_summary_check(self):
        orch = _make_orchestrator_state()
        cfg = _make_config(trigger=10)
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        calls = []

        def fake_maybe_run_summary_compaction(**kwargs):
            calls.append(kwargs.get("trigger_reason"))
            return False

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            kwargs["post_step_hook"]()
            return "done"

        with (
            patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()),
            patch.object(
                runner_module,
                "maybe_run_summary_compaction",
                side_effect=fake_maybe_run_summary_compaction,
            ),
        ):
            runner.run("subagent_worker", {"task": "long task"}, fake_run_agent)

        assert "subagent_post_step" in calls
        assert "subagent_pre_run" not in calls

    def test_summary_disabled_when_trigger_zero(self):
        orch = _make_orchestrator_state()
        cfg = _make_config(trigger=0)
        runner = SubagentRunner(_make_ctx(cfg), orch, cfg)
        captured = {}

        def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            captured.update(kwargs)
            return "ok"

        with (
            patch.object(runner, "_build_child_ctx", return_value=SimpleNamespace()),
            patch.object(runner_module, "maybe_run_summary_compaction", return_value=False) as mock_summary,
        ):
            runner.run("subagent_worker", {"task": "task"}, fake_run_agent)

        assert captured.get("persist_messages_callback") is None
        assert captured.get("post_step_hook") is None
        mock_summary.assert_not_called()
