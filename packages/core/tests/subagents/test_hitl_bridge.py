"""Step 5: child HITL emit (ask_human / confirm) + tui_confirm on child ctx."""
from __future__ import annotations
import pytest

from gc_testkit.helpers.tool_ctx import CallableTool

import json
from types import SimpleNamespace
from unittest.mock import MagicMock

from gc_core.tools.subagents.progress import QueueSubagentProgressSink, child_progress_enabled


def _child_ctx(sink, *, tool_name="subagent_worker"):
    return SimpleNamespace(
        run_id="child_1",
        parent_call_id="parent_fc",
        subagent_progress_sink=sink,
        subagent_tool_name=tool_name,
        is_tui=True,
        config_dict={},
        logger=None,
    )


class TestChildCtxCopiesTuiConfirm:
    @pytest.mark.asyncio
    async def test_build_child_ctx_copies_tui_confirm_and_tool_name(self, tmp_path):
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

        cfg = {
            "save_logs": True,
            "logs_dump_dir": str(tmp_path),
            "use_local_subagents": True,
            "backend_api_mode": "chat_completions",
            "enabled_tools": [],
            "skills_data_path": "",
            "mcp_servers": [],
            "model": "m",
            "system_prompt_id": "p",
            "chat_completions_params": {
                "auxiliary_llm": {"max_tokens": 128, "completion_kwargs": {}},
            },
        }
        confirm = MagicMock(return_value="once")
        from gc_testkit.helpers.tool_ctx import local_ctx

        parent = local_ctx(cfg)
        parent.run_id = "p1"
        parent.auth_connector = None
        parent.logger = None
        parent.logging_path = ""
        parent.summary_trace_path = ""
        parent.tools_data = {}
        parent.tools_spec = []
        parent.tools_description = []
        parent.is_tui = True
        parent.tui_confirm = confirm
        parent.permission_store = object()
        # Порт «спросить человека»: без него дочерний `turn_ask_human` читал
        # `input()` в потоке пула, а подмена stdin в TUI потоко-локальна —
        # поток делил настоящую консоль с Textual, и ход висел навсегда.
        ask_port = MagicMock(return_value="синий")
        parent.ask_human = ask_port
        runner = SubagentRunner(parent, state, cfg)
        runner._parent_call_id = "call_x"
        child = await runner._build_child_ctx(tool_name="subagent_worker")
        assert child.tui_confirm is confirm
        assert child.subagent_tool_name == "subagent_worker"
        assert getattr(child, "permission_store", None) is parent.permission_store
        assert getattr(child, "ask_human", None) is ask_port


class TestAskHumanEmit:
    def test_emits_ask_human_and_done_around_input(self, monkeypatch):
        from gc_core.tools.turn.ask_human import AskHuman

        sink = QueueSubagentProgressSink()
        ctx = _child_ctx(sink)
        state = SimpleNamespace(steps=3, max_steps=50)
        tool = CallableTool(AskHuman(ctx=ctx, state=state))

        answers = iter(["yes", ""])
        monkeypatch.setattr("builtins.input", lambda: next(answers))

        out = tool(question="Continue?")
        payload = json.loads(out)
        assert payload["ok"] is True

        evs = sink.drain()
        assert [e.kind for e in evs] == ["ask_human", "ask_human_done"]
        assert evs[0].prose == "Continue?"
        assert evs[0].tool_name == "subagent_worker"

    def test_parent_without_parent_call_id_emits_nothing(self, monkeypatch):
        from gc_core.tools.turn.ask_human import AskHuman

        sink = QueueSubagentProgressSink()
        ctx = SimpleNamespace(
            run_id="main",
            subagent_progress_sink=sink,
            is_tui=True,
            config_dict={},
            logger=None,
        )
        assert not child_progress_enabled(ctx)
        tool = CallableTool(AskHuman(ctx=ctx, state=SimpleNamespace(steps=1, max_steps=10)))
        answers = iter(["a", ""])
        monkeypatch.setattr("builtins.input", lambda: next(answers))
        tool(question="Parent ask?")
        assert sink.drain() == []


class TestConfirmEmit:
    def test_emits_confirm_around_tui_confirm(self):
        from gc_core.security.confirm_port import request_confirm

        sink = QueueSubagentProgressSink()
        seen = {}

        def tui_confirm(*, title, question, fields, via=None):
            seen["via"] = via
            seen["question"] = question
            return "once"

        ctx = _child_ctx(sink)
        ctx.tui_confirm = tui_confirm
        ctx.permission_store = None

        assert request_confirm(
            title="Write",
            question="Allow write?",
            fields=[("Path", "/x")],
            ctx=ctx,
        ) is True
        evs = sink.drain()
        assert [e.kind for e in evs] == ["confirm", "confirm_done"]
        assert evs[0].prose == "Allow write?"
        assert evs[0].tool_name == "subagent_worker"
        assert seen["via"] == "subagent_worker"
