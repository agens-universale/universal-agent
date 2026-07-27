"""Шаг 3: runtime persist call/result/error + in-memory detail cache (без sink)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from thread_store import read_subagent_trace
from tools.subagents.sidecar import (
    clear_detail_cache,
    lookup_tool_detail,
)


def _state(*, steps=3, max_steps=50):
    return SimpleNamespace(
        messages=[],
        steps=steps,
        max_steps=max_steps,
        session_compact_applied=False,
        tool_calls=0,
    )


def _child_ctx(tmp_path, *, sink=None):
    return SimpleNamespace(
        config_dict={},
        run_id="run_c1",
        parent_call_id="call_sub_1",
        subagent_progress_sink=sink,
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


def _patch_process(monkeypatch, *, apply_return='{"ok": true, "data": "hello"}'):
    import processing_responses as pr

    monkeypatch.setattr(
        pr,
        "apply_tool",
        lambda **kw: apply_return if isinstance(apply_return, str) else apply_return(kw),
    )
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")
    return pr


class TestSidecarPersistCallResult:
    def setup_method(self):
        clear_detail_cache()

    def test_persists_call_and_result_without_sink(self, tmp_path, monkeypatch):
        pr = _patch_process(monkeypatch)
        ctx = _child_ctx(tmp_path, sink=None)
        args = json.dumps({"path": "a.py"})
        pr.process_tool_calls(
            messages=[_fc("fs_read_file", "call_child_9", args)],
            state=_state(),
            ctx=ctx,
            run_agent_func=None,
        )

        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        assert [r["kind"] for r in rows] == ["call", "result"]
        assert rows[0]["parent_call_id"] == "call_sub_1"
        assert rows[0]["child_run_id"] == "run_c1"
        assert rows[0]["payload"]["name"] == "fs_read_file"
        assert rows[0]["payload"]["arguments"] == args
        assert rows[0]["payload"]["call_id"] == "call_child_9"
        assert rows[1]["payload"]["type"] == "function_call_output"
        assert rows[1]["payload"]["call_id"] == "call_child_9"
        assert "hello" in rows[1]["payload"]["output"]

        detail = lookup_tool_detail("call_child_9")
        assert detail is not None
        assert detail["call"]["arguments"] == args
        assert "hello" in detail["result"]["output"]

    def test_no_persist_without_thread_identity(self, tmp_path, monkeypatch):
        pr = _patch_process(monkeypatch)
        ctx = SimpleNamespace(
            config_dict={},
            run_id="run_c1",
            parent_call_id="call_sub_1",
            subagent_progress_sink=None,
            logger=None,
        )
        pr.process_tool_calls(
            messages=[_fc("fs_read_file", "c1")],
            state=_state(),
            ctx=ctx,
            run_agent_func=None,
        )
        assert read_subagent_trace(str(tmp_path), "cli", "task-1") == []
        assert lookup_tool_detail("c1") is None

    def test_error_kind_on_ok_false(self, tmp_path, monkeypatch):
        pr = _patch_process(
            monkeypatch,
            apply_return='{"ok": false, "error": {"message": "boom"}}',
        )
        pr.process_tool_calls(
            messages=[_fc("fs_read_file", "c_err")],
            state=_state(),
            ctx=_child_ctx(tmp_path),
            run_agent_func=None,
        )
        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        assert [r["kind"] for r in rows] == ["call", "error"]
        assert rows[1]["payload"]["call_id"] == "c_err"

    def test_error_kind_on_wrong_arguments(self, tmp_path, monkeypatch):
        import processing_responses as pr
        from tools import WrongFunctionArgumentsException

        def _boom(**kw):
            raise WrongFunctionArgumentsException("bad args", received_args="{}")

        monkeypatch.setattr(pr, "apply_tool", _boom)
        monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
        monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
        monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

        try:
            pr.process_tool_calls(
                messages=[_fc("fs_read_file", "c_bad")],
                state=_state(),
                ctx=_child_ctx(tmp_path),
                run_agent_func=None,
            )
            assert False, "expected WrongFunctionArgumentsException"
        except WrongFunctionArgumentsException:
            pass

        rows = read_subagent_trace(str(tmp_path), "cli", "task-1")
        assert [r["kind"] for r in rows] == ["call", "error"]
        assert "bad args" in rows[1]["payload"]["output"]

    def test_ask_human_not_persisted(self, tmp_path, monkeypatch):
        from tools.subagents.progress import QueueSubagentProgressSink

        pr = _patch_process(monkeypatch, apply_return='{"ok": true, "answer": "yes"}')
        sink = QueueSubagentProgressSink()
        pr.process_tool_calls(
            messages=[_fc("turn_ask_human", "ah1", '{"question": "ok?"}'),],
            state=_state(),
            ctx=_child_ctx(tmp_path, sink=sink),
            run_agent_func=None,
        )
        assert read_subagent_trace(str(tmp_path), "cli", "task-1") == []
        # Nested tool-row для HITL не монтируем (нет detail → Ctrl+click no-op).
        kinds = [e.kind for e in sink.drain()]
        assert "tool_start" not in kinds
        assert "tool_end" not in kinds

    def test_nested_not_appended_to_state_messages(self, tmp_path, monkeypatch):
        pr = _patch_process(monkeypatch)
        state = _state()
        pr.process_tool_calls(
            messages=[_fc("fs_read_file", "c1", '{"path": "x"}')],
            state=state,
            ctx=_child_ctx(tmp_path),
            run_agent_func=None,
        )
        # process_tool_calls returns outputs; state.messages не трогает sidecar
        assert state.messages == []
        assert read_subagent_trace(str(tmp_path), "cli", "task-1")
