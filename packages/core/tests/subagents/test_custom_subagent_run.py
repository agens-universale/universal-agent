"""Раннер с кастомным сабагентом: прокидывание allow/stop в дочерний ран."""

from types import SimpleNamespace

import pytest
from gc_core.run_context import acreate_run_context
from gc_core.tools.subagents.runner import SubagentRunner
from gc_testkit.settings import make_settings

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
        "paths.logs_dump_dir": str(tmp_path / "logs"),
        "logging.save_logs": True,
        "logging.format": "jsonl",
        "logging.console_trace": False,
        "backend.api_mode": "chat_completions",
        "backend.completions_url": "http://localhost:1/v1/chat/completions",
        "agent.model": "test-model",
        "launch_root": str(tmp_path),
        "agent.use_local_subagents": True,
        "agent.subagents_dir": agents_dir,
    }
    cfg.update(extra)
    return cfg


async def _runner(tmp_path, cfg, tool_name="subagent_alpha"):
    ctx = await acreate_run_context(make_settings(cfg))
    runner = SubagentRunner(ctx, SimpleNamespace(default_model="test-model"))
    runner._active_tool_name = tool_name
    return ctx, runner


class TestChildConfig:
    async def test_allow_and_stop_reach_child_config(self, tmp_path):
        agents = _agent_file(
            tmp_path,
            tools=("fs_read_file", *REQUIRED),
            stop_tools=("exec_python",),
        )
        _ctx, runner = await _runner(tmp_path, _config(tmp_path, agents))
        child = runner._build_child_config()
        assert child.runtime.subagent_tools_allow == (
            "fs_read_file",
            *REQUIRED,
        )
        assert child.runtime.subagent_tools_stop == ("exec_python",)
        # Глубина всегда 1.
        assert child.settings.agent.use_local_subagents is False

    async def test_child_inherits_the_channel_declarations(self, tmp_path):
        """Субагент работает в том же канале: «человека нет» и «слова
        внешние» доходят до его рантайма и с собственной областью тулов."""
        agents = _agent_file(tmp_path, tools=("fs_read_file", *REQUIRED))
        loaded = make_settings(_config(tmp_path, agents)).with_runtime(
            human_channel=False, external_user_words=True
        )
        ctx = await acreate_run_context(loaded)
        runner = SubagentRunner(
            ctx, SimpleNamespace(default_model="test-model")
        )
        runner._active_tool_name = "subagent_alpha"
        child = runner._build_child_config()
        assert child.runtime.human_channel is False
        assert child.runtime.external_user_words is True
        assert child.runtime.subagent_tools_allow == (
            "fs_read_file",
            *REQUIRED,
        )

    async def test_absent_tools_key_means_all_allowed(self, tmp_path):
        agents = _agent_file(tmp_path)
        _ctx, runner = await _runner(tmp_path, _config(tmp_path, agents))
        child = runner._build_child_config()
        assert child.runtime.subagent_tools_allow is None
        assert child.runtime.subagent_tools_stop == ()

    async def test_builtin_subagent_gets_no_filter(self, tmp_path):
        agents = _agent_file(tmp_path)
        ctx, runner = await _runner(
            tmp_path, _config(tmp_path, agents), tool_name="subagent_worker"
        )
        child = runner._build_child_config()
        # Встроенный наследует рантайм родителя как есть — своей области нет.
        assert child.runtime == ctx.runtime

    async def test_file_stop_tools_add_to_global_not_replace(self, tmp_path):
        # Глобальный вычитается внутри get_builtin_tools_data, файловый — поверх
        # полного набора. Снять глобальный запрет из файла нельзя.
        agents = _agent_file(tmp_path, stop_tools=("exec_command",))
        cfg = _config(
            tmp_path, agents, **{"agent.stop_tools": ["exec_python"]}
        )
        _ctx, runner = await _runner(tmp_path, cfg)
        child = runner._build_child_config()
        assert list(child.settings.agent.stop_tools) == ["exec_python"]
        assert child.runtime.subagent_tools_stop == ("exec_command",)

        from gc_core.tools import abuild_tools_data

        data, _spec, _descr, _mcp = await abuild_tools_data(child)
        assert "exec_python" not in data  # глобальный запрет
        assert "exec_command" not in data  # запрет из файла


@pytest.mark.asyncio
async def test_both_build_sites_agree(tmp_path):
    """_build_child_state и _build_child_ctx обязаны видеть одни и те же настройки.

    Раньше словарь собирался в двух местах независимо — именно этот класс
    рассинхрона уже приводил к расхождению ctx.trace_id и конфига.
    """
    agents = _agent_file(tmp_path, tools=("fs_read_file", *REQUIRED))
    cfg = _config(tmp_path, agents)
    _ctx, runner = await _runner(tmp_path, cfg)

    child_ctx = await runner._build_child_ctx("subagent_alpha")
    state_config = runner._build_child_config()

    assert child_ctx.runtime == state_config.runtime
    assert (
        child_ctx.settings.agent.use_local_subagents
        is state_config.settings.agent.use_local_subagents
        is False
    )


@pytest.mark.asyncio
async def test_child_tools_are_actually_narrowed(tmp_path):
    agents = _agent_file(tmp_path, tools=("fs_read_file", *REQUIRED))
    _ctx, runner = await _runner(tmp_path, _config(tmp_path, agents))
    child_ctx = await runner._build_child_ctx("subagent_alpha")
    names = {s.get("name") for s in child_ctx.tools_spec}
    assert names == {"fs_read_file", *REQUIRED}
