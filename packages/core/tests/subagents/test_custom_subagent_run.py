"""Раннер с кастомным сабагентом: прокидывание allow/stop в дочерний ран."""
from types import SimpleNamespace

from gc_core.run_context import create_run_context
from gc_core.tools import SUBAGENT_TOOLS_ALLOW_KEY, SUBAGENT_TOOLS_STOP_KEY
from gc_core.tools.subagents.runner import SubagentRunner

REQUIRED = (
    "turn_finish",
    "session_read_stored_tool_output",
    "session_memory",
    "session_todo",
)


def _agent_file(tmp_path, name="subagent_alpha", tools=None, stop_tools=()):
    agents = tmp_path / "agents"
    agents.mkdir(exist_ok=True)
    lines = [f"name: {name}", f"description: Описание {name}."]
    if tools is not None:
        lines.append("tools: [" + ", ".join(tools) + "]")
    if stop_tools:
        lines.append("stop_tools: [" + ", ".join(stop_tools) + "]")
    (agents / f"{name}.md").write_text(
        "---\n" + "\n".join(lines) + "\n---\nТы — альфа.\n", encoding="utf-8"
    )
    return str(agents)


def _config(tmp_path, agents_dir, **extra):
    cfg = {
        "logs_dump_dir": str(tmp_path / "logs"),
        "save_logs": True,
        "logging_format": "jsonl",
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
        "use_local_subagents": True,
        "subagents_dir": agents_dir,
    }
    cfg.update(extra)
    return cfg


def _runner(tmp_path, cfg, tool_name="subagent_alpha"):
    ctx = create_run_context(cfg)
    runner = SubagentRunner(ctx, SimpleNamespace(default_model="test-model"), cfg)
    runner._active_tool_name = tool_name
    return ctx, runner


class TestChildConfig:
    def test_allow_and_stop_reach_child_config(self, tmp_path):
        agents = _agent_file(
            tmp_path,
            tools=("fs_read_file", *REQUIRED),
            stop_tools=("exec_python",),
        )
        _ctx, runner = _runner(tmp_path, _config(tmp_path, agents))
        child = runner._build_child_config()
        assert child[SUBAGENT_TOOLS_ALLOW_KEY] == ["fs_read_file", *REQUIRED]
        assert child[SUBAGENT_TOOLS_STOP_KEY] == ["exec_python"]
        # Глубина всегда 1.
        assert child["use_local_subagents"] is False

    def test_absent_tools_key_means_all_allowed(self, tmp_path):
        agents = _agent_file(tmp_path)
        _ctx, runner = _runner(tmp_path, _config(tmp_path, agents))
        assert runner._build_child_config()[SUBAGENT_TOOLS_ALLOW_KEY] is None

    def test_builtin_subagent_gets_no_filter(self, tmp_path):
        agents = _agent_file(tmp_path)
        _ctx, runner = _runner(tmp_path, _config(tmp_path, agents), tool_name="subagent_worker")
        child = runner._build_child_config()
        assert SUBAGENT_TOOLS_ALLOW_KEY not in child

    def test_file_stop_tools_add_to_global_not_replace(self, tmp_path):
        # Глобальный stop_tools режет слитый набор в build_tools_data, файловый —
        # поверх. Снять глобальный запрет из файла нельзя.
        agents = _agent_file(tmp_path, stop_tools=("exec_command",))
        cfg = _config(tmp_path, agents, stop_tools=["exec_python"])
        _ctx, runner = _runner(tmp_path, cfg)
        child = runner._build_child_config()
        assert child["stop_tools"] == ["exec_python"]
        assert child[SUBAGENT_TOOLS_STOP_KEY] == ["exec_command"]

        from gc_core.tools import build_tools_data

        data, _spec, _descr, _mcp = build_tools_data(child)
        assert "exec_python" not in data   # глобальный запрет
        assert "exec_command" not in data  # запрет из файла


def test_both_build_sites_agree(tmp_path):
    """_build_child_state и _build_child_ctx обязаны видеть один и тот же конфиг.

    Раньше словарь собирался в двух местах независимо — именно этот класс
    рассинхрона уже приводил к расхождению ctx.trace_id и config_dict.
    """
    agents = _agent_file(tmp_path, tools=("fs_read_file", *REQUIRED))
    cfg = _config(tmp_path, agents)
    _ctx, runner = _runner(tmp_path, cfg)

    child_ctx = runner._build_child_ctx("subagent_alpha")
    state_config = runner._build_child_config()

    for key in (SUBAGENT_TOOLS_ALLOW_KEY, SUBAGENT_TOOLS_STOP_KEY, "use_local_subagents"):
        assert child_ctx.config_dict.get(key) == state_config.get(key)


def test_child_tools_are_actually_narrowed(tmp_path):
    agents = _agent_file(tmp_path, tools=("fs_read_file", *REQUIRED))
    _ctx, runner = _runner(tmp_path, _config(tmp_path, agents))
    child_ctx = runner._build_child_ctx("subagent_alpha")
    names = {s.get("name") for s in child_ctx.tools_spec}
    assert names == {"fs_read_file", *REQUIRED}
