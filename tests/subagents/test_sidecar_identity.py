"""Шаг 2: thread identity на child ctx + gate sidecar без sink."""

from __future__ import annotations

from types import SimpleNamespace

from tools.subagents.progress import QueueSubagentProgressSink, child_progress_enabled
from tools.subagents.sidecar import (
    bind_thread_identity,
    clear_thread_identity,
    resolve_sidecar_identity,
)


class TestResolveSidecarIdentityGate:
    def test_enabled_with_identity_and_parent_call_id_without_sink(self):
        ctx = SimpleNamespace(
            parent_call_id="call_sub_1",
            run_id="run_c1",
            threads_dir="/tmp/threads",
            system_id="cli",
            thread_id="task-1",
            subagent_progress_sink=None,
        )
        ident = resolve_sidecar_identity(ctx)
        assert ident is not None
        assert ident.threads_dir == "/tmp/threads"
        assert ident.system_id == "cli"
        assert ident.thread_id == "task-1"
        assert ident.parent_call_id == "call_sub_1"
        assert ident.child_run_id == "run_c1"
        assert not child_progress_enabled(ctx)

    def test_enabled_even_when_sink_present(self):
        sink = QueueSubagentProgressSink()
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="t1",
            subagent_progress_sink=sink,
        )
        assert resolve_sidecar_identity(ctx) is not None
        assert child_progress_enabled(ctx)

    def test_disabled_without_parent_call_id(self):
        ctx = SimpleNamespace(
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="t1",
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_disabled_without_threads_dir(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            system_id="cli",
            thread_id="t1",
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_disabled_without_thread_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id=None,
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_disabled_on_invalid_thread_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="cli",
            thread_id="bad id!",
        )
        assert resolve_sidecar_identity(ctx) is None

    def test_sanitizes_system_id(self):
        ctx = SimpleNamespace(
            parent_call_id="p1",
            run_id="c1",
            threads_dir="/t",
            system_id="CLI",
            thread_id="t1",
        )
        ident = resolve_sidecar_identity(ctx)
        assert ident is not None
        assert ident.system_id == "cli"


class TestBindClearThreadIdentity:
    def test_bind_and_clear(self):
        ctx = SimpleNamespace()
        bind_thread_identity(
            ctx, threads_dir="/threads", system_id="cli", thread_id="abc"
        )
        assert ctx.threads_dir == "/threads"
        assert ctx.system_id == "cli"
        assert ctx.thread_id == "abc"
        clear_thread_identity(ctx)
        assert ctx.threads_dir is None
        assert ctx.system_id is None
        assert ctx.thread_id is None


class TestChildCtxCopiesThreadIdentity:
    def test_build_child_ctx_copies_identity_without_sink(self, tmp_path):
        from states import SharedStateData, State
        from tests.helpers.tool_ctx import local_ctx
        from tools.subagents.runner import SubagentRunner

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
                "llm": {"max_tokens": 128, "completion_kwargs": {}},
            },
        }
        parent = local_ctx(cfg)
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
            parent,
            threads_dir=str(tmp_path / "threads"),
            system_id="cli",
            thread_id="task-9",
        )
        runner = SubagentRunner(parent, state, cfg)
        runner._parent_call_id = "call_sub"
        child = runner._build_child_ctx(tool_name="subagent_worker")

        assert child.threads_dir == str(tmp_path / "threads")
        assert child.system_id == "cli"
        assert child.thread_id == "task-9"
        assert child.parent_call_id == "call_sub"
        assert getattr(child, "subagent_progress_sink", None) is None
        ident = resolve_sidecar_identity(child)
        assert ident is not None
        assert ident.parent_call_id == "call_sub"
        assert not child_progress_enabled(child)
