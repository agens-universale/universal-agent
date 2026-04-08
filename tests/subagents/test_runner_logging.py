from types import SimpleNamespace

from states import SharedStateData
from states import State
from states import save_state
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "read_file"}, {"name": "subagent_worker"}],
        tools_descr=["read desc", "worker desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="You are a helper.",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    save_state(state)
    return state


def _make_config(tmp_path):
    return {
        "save_logs": True,
        "logs_dump_dir": str(tmp_path),
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "subagent_max_steps": 30,
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "test-model",
        "system_prompt_id": "test-prompt",
    }


def test_build_child_ctx_creates_dedicated_subagent_run_log(tmp_path):
    orch = _make_orchestrator_state()
    cfg = _make_config(tmp_path)
    parent_ctx = SimpleNamespace(
        config_dict=cfg,
        run_id="parent_run_1",
        auth_connector=None,
        logger=None,
        logging_path=str(tmp_path / "agent_main_parent_run_1.log"),
        summary_trace_path=str(tmp_path / "summary_trace_main_parent_run_1.jsonl"),
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = runner._build_child_ctx(tool_name="subagent_worker")

    assert child_ctx.run_id != parent_ctx.run_id
    assert f"agent_worker_{child_ctx.run_id}.log" in child_ctx.logging_path


def test_lifecycle_payload_contains_parent_and_child_run_ids(tmp_path):
    orch = _make_orchestrator_state()
    cfg = _make_config(tmp_path)
    recorded = []

    class DummyLogger:
        def info(self, msg, payload):
            recorded.append((msg, payload))

    parent_ctx = SimpleNamespace(
        config_dict=cfg,
        run_id="parent_run_1",
        auth_connector=None,
        logger=DummyLogger(),
        logging_path="",
        summary_trace_path="",
        tools_data={},
        tools_spec=[],
        tools_description=[],
    )
    runner = SubagentRunner(parent_ctx, orch, cfg)
    child_state = runner._build_child_state("role")
    child_ctx = SimpleNamespace(run_id="child_run_2", logger=None)

    runner._log_lifecycle(
        event="subagent_start",
        tool_name="subagent_worker",
        child_state=child_state,
        child_ctx=child_ctx,
    )

    assert recorded
    payload = recorded[0][1]
    assert payload["parent_run_id"] == "parent_run_1"
    assert payload["child_run_id"] == "child_run_2"
    assert payload["subagent_tool_name"] == "subagent_worker"
