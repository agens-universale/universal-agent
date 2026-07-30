import json
import logging

from states import SharedStateData
from states import State
from tests.helpers.tool_ctx import local_ctx
from tools.subagents.runner import SubagentRunner


def _make_orchestrator_state():
    state = State(
        verbose=False,
        model="test-model",
        tools_spec=[{"name": "fs_read_file"}, {"name": "subagent_worker"}],
        tools_descr=["read desc", "worker desc"],
        skills_list=[],
        max_steps=100,
        system_prompt_id="test-prompt",
        use_server_subagents=False,
        chat_completions_system_prompt="You are a helper.",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
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
        "chat_completions_params": {
            "llm": {
                "max_tokens": 8192,
                "completion_kwargs": {},
            },
        },
    }


def _parent_ctx(tmp_path, *, is_tui: bool = False, is_cli_mode: bool = False):
    cfg = _make_config(tmp_path)
    ctx = local_ctx(cfg)
    ctx.run_id = "parent_run_1"
    ctx.auth_connector = None
    ctx.logger = None
    ctx.logging_path = str(tmp_path / "agent_main_parent_run_1.log")
    ctx.summary_trace_path = str(tmp_path / "summary_trace_main_parent_run_1.jsonl")
    ctx.tools_data = {}
    ctx.tools_spec = []
    ctx.tools_description = []
    ctx.is_tui = is_tui
    ctx.is_cli_mode = is_cli_mode
    return ctx, cfg


def test_build_child_ctx_creates_dedicated_subagent_run_log(tmp_path):
    orch = _make_orchestrator_state()
    parent_ctx, cfg = _parent_ctx(tmp_path)
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = runner._build_child_ctx(tool_name="subagent_worker")

    assert child_ctx.run_id != parent_ctx.run_id
    assert f"agent_worker_{child_ctx.run_id}.log" in child_ctx.logging_path
    assert child_ctx.workspace is parent_ctx.workspace


def test_build_child_ctx_inherits_is_tui_from_parent(tmp_path):
    orch = _make_orchestrator_state()
    parent_ctx, cfg = _parent_ctx(tmp_path, is_tui=True)
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = runner._build_child_ctx(tool_name="subagent_worker")

    assert child_ctx.is_tui is True


def test_build_child_ctx_inherits_is_cli_mode_from_parent(tmp_path):
    orch = _make_orchestrator_state()
    parent_ctx, cfg = _parent_ctx(tmp_path, is_cli_mode=True)
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = runner._build_child_ctx(tool_name="subagent_worker")

    assert child_ctx.is_cli_mode is True
    assert child_ctx.is_tui is False


def test_build_child_ctx_cli_mode_defaults_false_when_parent_unset(tmp_path):
    orch = _make_orchestrator_state()
    parent_ctx, cfg = _parent_ctx(tmp_path)
    if hasattr(parent_ctx, "is_cli_mode"):
        delattr(parent_ctx, "is_cli_mode")
    runner = SubagentRunner(parent_ctx, orch, cfg)

    child_ctx = runner._build_child_ctx(tool_name="subagent_worker")

    assert child_ctx.is_cli_mode is False


def test_lifecycle_payload_contains_parent_and_child_run_ids(tmp_path):
    from types import SimpleNamespace

    orch = _make_orchestrator_state()
    parent_ctx, cfg = _parent_ctx(tmp_path)
    recorded: list[str] = []

    class _CaptureHandler(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            recorded.append(record.getMessage())

    parent_log = logging.getLogger("subagent_lifecycle_test_parent")
    parent_log.handlers.clear()
    parent_log.addHandler(_CaptureHandler())
    parent_log.setLevel(logging.INFO)
    parent_ctx.logger = parent_log

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
    prefix, sep, json_blob = recorded[0].partition(" ")
    assert prefix == "subagent_lifecycle"
    payload = json.loads(json_blob)
    assert payload["parent_run_id"] == "parent_run_1"
    assert payload["child_run_id"] == "child_run_2"
    assert payload["subagent_tool_name"] == "subagent_worker"
