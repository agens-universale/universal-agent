"""Шаг 4: sidecar intermediate_reply + finished + child_summary."""

from __future__ import annotations

import json
from types import SimpleNamespace
from unittest.mock import patch

from gc_core.states import SharedStateData, State
from gc_core.thread_store import read_subagent_trace
from gc_core.tools.subagents.sidecar import apersist_subagent_trace
from gc_testkit import run_coro


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def _state():
    return SimpleNamespace(
        messages=[],
        steps=2,
        max_steps=50,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
    )


def _child_ctx(tmp_path):
    return SimpleNamespace(
        config_dict={},
        run_id="run_c1",
        parent_call_id="call_sub_1",
        subagent_progress_sink=None,
        logger=None,
        threads_dir=str(tmp_path),
        system_id="cli",
        thread_id="task-1",
    )


def _fc(name: str, call_id: str, arguments: str = "{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _patch_process(monkeypatch, *, apply_return):
    from gc_core import processing_responses as pr

    async def _fake_aapply_tool(**kw):
        return apply_return

    monkeypatch.setattr(pr, "aapply_tool", _fake_aapply_tool)
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        _async_noop(lambda **kw: None),
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")
    return pr


def _orch_state():
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
    return state


def _runner_cfg(tmp_path, *, trigger=0):
    return {
        "save_logs": True,
        "logs_dump_dir": str(tmp_path),
        "use_local_subagents": True,
        "backend_api_mode": "chat_completions",
        "subagent_max_steps": 30,
        "skills_data_path": "",
        "model": "m",
        "system_prompt_id": "p",
        "summary_params": {
            "chat_completions_summary_trigger_chars": trigger,
        },
        "chat_completions_params": {
            "auxiliary_llm": {"max_tokens": 128, "completion_kwargs": {}},
        },
    }


class TestSidecarIntermediateReply:
    def test_persists_intermediate_reply_without_sink(
        self, tmp_path, monkeypatch
    ):
        answer = "Сейчас проверю структуру"
        pr = _patch_process(
            monkeypatch,
            apply_return=json.dumps(
                {
                    "tool": "turn_intermediate_reply",
                    "ok": True,
                    "answer": answer,
                }
            ),
        )
        run_coro(
            pr.aprocess_tool_calls(
                messages=[
                    _fc(
                        "turn_intermediate_reply",
                        "ir1",
                        json.dumps({"answer": answer}),
                    )
                ],
                state=_state(),
                ctx=_child_ctx(tmp_path),
                run_agent_func=None,
            )
        )
        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        assert [r["kind"] for r in rows] == ["intermediate_reply"]
        assert rows[0]["payload"]["prose"] == answer
        assert rows[0]["payload"]["call_id"] == "ir1"


class TestSidecarFinished:
    async def test_finished_on_success(self, tmp_path):
        from gc_core.tools.subagents.runner import SubagentRunner
        from gc_core.tools.subagents.sidecar import bind_thread_identity
        from gc_testkit.helpers.tool_ctx import local_ctx

        cfg = _runner_cfg(tmp_path)
        parent = local_ctx(cfg)
        parent.run_id = "p1"
        parent.auth_connector = None
        parent.logger = None
        parent.logging_path = ""
        parent.summary_trace_path = ""
        parent.tools_data = {}
        parent.tools_spec = []
        parent.tools_description = []
        bind_thread_identity(
            parent,
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id="task-1",
        )
        runner = SubagentRunner(parent, _orch_state(), cfg)

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.steps = 3
            return "done"

        result = await runner.arun(
            "subagent_worker",
            {"task": "x", "reason": "y"},
            fake_run_agent,
            parent_call_id="call_sub_1",
        )
        assert json.loads(result)["ok"] is True
        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        finished = [r for r in rows if r["kind"] == "finished"]
        assert len(finished) == 1
        assert finished[0]["payload"] == {
            "ok": True,
            "finish_reason": "completed",
            "steps": 3,
        }
        assert finished[0]["parent_call_id"] == "call_sub_1"

    async def test_finished_on_user_stop(self, tmp_path):
        from gc_core.tools.subagents.runner import SubagentRunner
        from gc_core.tools.subagents.sidecar import bind_thread_identity
        from gc_testkit.helpers.tool_ctx import local_ctx

        cfg = _runner_cfg(tmp_path)
        parent = local_ctx(cfg)
        parent.run_id = "p1"
        parent.auth_connector = None
        parent.logger = None
        parent.logging_path = ""
        parent.summary_trace_path = ""
        parent.tools_data = {}
        parent.tools_spec = []
        parent.tools_description = []
        bind_thread_identity(
            parent,
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id="task-1",
        )
        runner = SubagentRunner(parent, _orch_state(), cfg)

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.steps = 1
            return None

        await runner.arun(
            "subagent_worker",
            {"task": "x", "reason": "y"},
            fake_run_agent,
            parent_call_id="call_sub_1",
        )
        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        finished = [r for r in rows if r["kind"] == "finished"]
        assert len(finished) == 1
        assert finished[0]["payload"]["ok"] is False
        assert finished[0]["payload"]["finish_reason"] == "stopped_by_user"


class TestSidecarChildSummary:
    async def test_child_summary_on_compaction_applied(self, tmp_path):
        from gc_core import run_summary_coordinator as rsc
        from gc_core.tools.subagents.runner import SubagentRunner
        from gc_core.tools.subagents.sidecar import bind_thread_identity
        from gc_testkit.helpers.tool_ctx import local_ctx

        cfg = _runner_cfg(tmp_path, trigger=10)
        parent = local_ctx(cfg)
        parent.run_id = "p1"
        parent.auth_connector = None
        parent.logger = None
        parent.logging_path = ""
        parent.summary_trace_path = ""
        parent.tools_data = {}
        parent.tools_spec = []
        parent.tools_description = []
        bind_thread_identity(
            parent,
            threads_dir=str(tmp_path),
            system_id="cli",
            thread_id="task-1",
        )
        runner = SubagentRunner(parent, _orch_state(), cfg)

        async def fake_run_summary_compaction(**kwargs):
            # Двойник обязан вести себя как настоящий шов: колбэк по контракту
            # возвращает None ЛИБО awaitable, и ждать его — часть контракта.
            from gc_core.utils.aio_compat import await_callback

            on_applied = kwargs.get("on_summary_applied")
            assert on_applied is not None
            await await_callback(
                on_applied(
                    {
                        "trigger_reason": "subagent_pre_backend",
                        "summary_payload": {
                            "summary_message_content": "wrapped",
                        },
                        "summary_trace": {
                            "final_output": {
                                "agent_summary_text": "сжатый контекст"
                            },
                        },
                        "newly_summarized_message_id": "m1",
                    }
                )
            )

        async def fake_run_agent(messages, child_state, child_ctx, **kwargs):
            child_state.messages = [
                {"role": "user", "content": "old"},
                {"role": "user", "content": "new"},
            ]
            # строки до маркера остаются в sidecar
            await apersist_subagent_trace(
                child_ctx,
                kind="call",
                payload={
                    "type": "function_call",
                    "name": "fs_read_file",
                    "call_id": "before",
                    "arguments": "{}",
                },
            )
            # Хук корутинный (см. runner.py) — ждём его.
            await kwargs["pre_backend_compaction_hook"](
                [{"role": "user", "content": "new"}],
                "user_ask",
            )
            return "ok"

        with (
            patch.object(rsc, "exceeded_summary_triggers", return_value=True),
            patch.object(
                rsc,
                "arun_summary_compaction",
                side_effect=fake_run_summary_compaction,
            ),
        ):
            await runner.arun(
                "subagent_worker",
                {"task": "long", "reason": "y"},
                fake_run_agent,
                parent_call_id="call_sub_1",
            )

        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        kinds = [r["kind"] for r in rows]
        assert "child_summary" in kinds
        assert "finished" in kinds
        summary = next(r for r in rows if r["kind"] == "child_summary")
        assert summary["payload"]["synthesis"] == "сжатый контекст"
        # компакция не удаляет уже записанное
        call_idx = next(
            i
            for i, r in enumerate(rows)
            if r["kind"] == "call" and r["payload"].get("call_id") == "before"
        )
        assert call_idx < kinds.index("child_summary")
