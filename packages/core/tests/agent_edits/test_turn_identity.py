"""turn_id: идентичность хода для журнала правок.

Один RunContext живёт на всю TUI-сессию (executor кэшируется), поэтому
ctx.run_id не годится как идентификатор хода — нужен отдельный turn_id,
свежий на каждый вызов AgentExecutor.run(), независимо от наличия треда.
"""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

import gc_core.agent_executor as agent_executor_module
from gc_core.agent_edits.journal import EditsJournal
from gc_core.config_modules import env_vars
from gc_core.run_context import acreate_run_context
from gc_core.thread_store import edits_journal_path
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    bind_turn_identity,
    clear_turn_identity,
    current_turn_id,
    mint_turn_id,
)


class TestMintBindClearTurnIdentity:
    def test_mint_returns_12_char_hex(self):
        turn_id = mint_turn_id()
        assert len(turn_id) == 12
        assert all(c in "0123456789abcdef" for c in turn_id)

    def test_mint_is_unique_across_calls(self):
        assert mint_turn_id() != mint_turn_id()

    def test_bind_and_current(self):
        ctx = SimpleNamespace()
        assert current_turn_id(ctx) is None
        bind_turn_identity(ctx, "abc123abc123")
        assert ctx.turn_id == "abc123abc123"
        assert current_turn_id(ctx) == "abc123abc123"

    def test_clear(self):
        ctx = SimpleNamespace()
        bind_turn_identity(ctx, "abc123abc123")
        clear_turn_identity(ctx)
        assert ctx.turn_id is None
        assert current_turn_id(ctx) is None


def _prompt_file(tmp_path):
    path = tmp_path / "system_prompt.md"
    if not path.exists():
        path.write_text("SYSTEM PROMPT", encoding="utf-8")
    return path


def _config(tmp_path):
    return {
        "logs_dump_dir": str(tmp_path / "logs"),
        "threads_data_path": str(tmp_path / "threads"),
        "save_logs": True,
        "logging_format": "jsonl",
        "console_trace": False,
        "backend_api_mode": "chat_completions",
        "completions_url": "http://localhost:1/v1/chat/completions",
        "model": "test-model",
        "launch_root": str(tmp_path),
        "run_mode": "cli",
        "agent_edits": {"record": {"cli": True}},
        "max_steps": 0,
        "system_prompt_id": "v1",
        "chat_completions_system_prompt_path": str(_prompt_file(tmp_path)),
        "no_tool_call_max_count": 5,
        "verbose": False,
    }


class TestAgentExecutorBindsFreshTurnIdPerRun:
    """AgentExecutor.arun() биндит новый turn_id на каждый вызов (тот же ctx,
    тот же executor — как в TUI, где RunContext кэшируется на всю сессию)."""

    async def test_each_run_gets_fresh_turn_id_without_thread(self, tmp_path):
        ctx = await acreate_run_context(_config(tmp_path))
        seen = []

        async def _fake_run_agent(messages, state, _ctx, **_kwargs):
            seen.append(current_turn_id(_ctx))
            state.messages += list(messages)
            return "ok"

        with patch.object(
            agent_executor_module, "arun_agent", side_effect=_fake_run_agent
        ):
            executor = agent_executor_module.AgentExecutor(ctx)
            await executor.arun(["первый ход"], verbose=False)
            await executor.arun(["второй ход"], verbose=False)

        assert len(seen) == 2
        assert seen[0] and seen[1]
        assert seen[0] != seen[1]

    async def test_each_run_gets_fresh_turn_id_with_same_thread(
        self, tmp_path
    ):
        ctx = await acreate_run_context(_config(tmp_path))
        seen = []

        async def _fake_run_agent(messages, state, _ctx, **_kwargs):
            seen.append(current_turn_id(_ctx))
            state.messages += list(messages)
            return "ok"

        with patch.object(
            agent_executor_module, "arun_agent", side_effect=_fake_run_agent
        ):
            executor = agent_executor_module.AgentExecutor(ctx)
            await executor.arun(
                ["первый ход"],
                thread_id="thr-turn",
                system="cli",
                verbose=False,
            )
            await executor.arun(
                ["второй ход"],
                thread_id="thr-turn",
                system="cli",
                verbose=False,
            )

        assert len(seen) == 2
        assert seen[0] and seen[1]
        assert seen[0] != seen[1]
        # thread identity продолжает жить рядом с turn identity, не вытесняется ей
        assert ctx.thread_id == "thr-turn"


def _subagent_parent_ctx(tmp_path, monkeypatch, *, turn_id: str | None):
    """Родительский ctx оркестратора с тредом (и, опционально, ходом)."""
    from gc_testkit.helpers.tool_ctx import local_ctx

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()

    cfg = {
        "save_logs": True,
        "logs_dump_dir": str(tmp_path / "logs"),
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "enabled_tools": [],
        "skills_data_path": "",
        "mcp_servers": [],
        "model": "m",
        "system_prompt_id": "p",
        "launch_root": str(launch_root),
        "chat_completions_params": {
            "llm": {"max_tokens": 128, "completion_kwargs": {}}
        },
    }
    parent = local_ctx(cfg)
    # Режим с записью правок: дочерний ctx субагента наследует его от родителя.
    parent.is_tui = True
    parent.run_id = "p1"
    parent.auth_connector = None
    parent.logger = None
    parent.logging_path = ""
    parent.summary_trace_path = ""
    parent.tools_data = {}
    parent.tools_spec = []
    parent.tools_description = []
    parent.subagent_progress_sink = None
    bind_thread_identity(
        parent, threads_dir=str(threads_dir), system_id="cli", thread_id="th1"
    )
    if turn_id is not None:
        bind_turn_identity(parent, turn_id)
    return parent, cfg, launch_root, threads_dir


async def _child_ctx(parent, cfg):
    from gc_core.states import SharedStateData, State
    from gc_core.tools.subagents.runner import SubagentRunner

    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="p",
        use_server_subagents=False,
        chat_completions_system_prompt="s",
        shared=SharedStateData(),
    )
    state.backend_api_mode = "chat_completions"
    runner = SubagentRunner(parent, state, cfg)
    runner._parent_call_id = "call_sub"
    return await runner._build_child_ctx(tool_name="subagent_worker")


class TestSubagentEditsCarryParentTurnId:
    """Спека §5: правки субагента ложатся в журнал ходом РОДИТЕЛЯ."""

    async def test_child_ctx_inherits_parent_turn_id(
        self, tmp_path, monkeypatch
    ):
        parent, cfg, _launch_root, _threads_dir = _subagent_parent_ctx(
            tmp_path, monkeypatch, turn_id="turn-parent"
        )
        child = await _child_ctx(parent, cfg)

        assert current_turn_id(child) == "turn-parent"

    async def test_subagent_edit_lands_with_parent_turn_id(
        self, tmp_path, monkeypatch
    ):
        parent, cfg, launch_root, threads_dir = _subagent_parent_ctx(
            tmp_path, monkeypatch, turn_id="turn-parent"
        )
        child = await _child_ctx(parent, cfg)
        target = launch_root / "child.py"

        tool = WriteToFile(
            ctx=child, config_dict=child.config_dict, call_id="call-child"
        )
        raw = await tool.acall(path=str(target), content="hello\n")
        assert json.loads(raw)["ok"] is True

        recs = EditsJournal(
            edits_journal_path(str(threads_dir), "cli", "th1")
        ).read_all()
        assert [r.turn_id for r in recs] == ["turn-parent"]
        # call_id — СВОЙ, вложенного тула: по нему интерфейс находит запись и
        # рисует инлайн-дифф под nested-строкой (EditsLookup.by_call_id).
        assert [r.call_id for r in recs] == ["call-child"]

    async def test_child_ctx_without_parent_turn_id_stays_empty(
        self, tmp_path, monkeypatch
    ):
        parent, cfg, _launch_root, _threads_dir = _subagent_parent_ctx(
            tmp_path, monkeypatch, turn_id=None
        )
        child = await _child_ctx(parent, cfg)

        assert current_turn_id(child) is None
