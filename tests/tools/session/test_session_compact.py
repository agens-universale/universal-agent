import json
import types
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest

from states import State
from tools.registry import get_builtin_tools_data
import tools.session.session_compact as session_compact_module
from tools.session.session_compact import SessionCompact, TRIGGER_REASON


def _state(*, backend_api_mode="chat_completions", messages=None):
    return types.SimpleNamespace(
        backend_api_mode=backend_api_mode,
        messages=messages
        if messages is not None
        else [{"role": "user", "content": "hello", "id": "u1"}],
        thread_id="t1",
        user_intent_log=[],
        current_user_intent=[],
        dialog_steps_offset=0,
        shared=types.SimpleNamespace(working_memory={}),
    )


def test_registered_only_for_chat_completions():
    assert "session_compact" not in get_builtin_tools_data({"backend_api_mode": "responses"})
    tools = get_builtin_tools_data({"backend_api_mode": "chat_completions"})
    assert "session_compact" in tools
    assert tools["session_compact"]["tool_fabric"] is SessionCompact


def test_session_compact_calls_run_summary_compaction(monkeypatch):
    state = _state()
    ctx = types.SimpleNamespace(
        config_dict={},
        logger=None,
        on_summary_applied=MagicMock(),
        is_cli_mode=True,
    )
    captured = {}

    def fake_compact(**kwargs):
        captured.update(kwargs)
        state.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "summary body", "id": "sum"},
        ]

    monkeypatch.setattr(session_compact_module, "run_summary_compaction", fake_compact)

    tool = SessionCompact(ctx=ctx, state=state)
    res = json.loads(tool(reason="phase done"))

    assert res["ok"] is True
    assert res["compacted"] is True
    assert captured["ctx"] is ctx
    assert captured["state"] is state
    assert captured["trigger_reason"] == TRIGGER_REASON
    assert captured["is_cli_mode"] is True
    assert captured["on_summary_applied"] is ctx.on_summary_applied
    assert state.session_compact_applied is True


def test_session_compact_rejects_empty_reason():
    res = json.loads(SessionCompact(state=_state())(reason="  "))
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_argument"
    assert res["error"]["field"] == "reason"


def test_session_compact_rejects_responses_backend():
    res = json.loads(
        SessionCompact(state=_state(backend_api_mode="responses"))(reason="phase done")
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "unsupported_backend"


def test_session_compact_rejects_empty_dialog():
    state = _state(messages=[{"role": "system", "content": "only sys"}])
    res = json.loads(SessionCompact(state=state)(reason="cleanup"))
    assert res["ok"] is False
    assert res["error"]["type"] == "nothing_to_compact"


def test_session_compact_reports_compaction_failure(monkeypatch):
    state = _state()
    ctx = types.SimpleNamespace(config_dict={}, logger=None)

    def boom(**_kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(session_compact_module, "run_summary_compaction", boom)

    res = json.loads(SessionCompact(ctx=ctx, state=state)(reason="retry"))
    assert res["ok"] is False
    assert res["error"]["type"] == "compaction_failed"
    assert "llm down" in res["error"]["message"]


def test_process_tool_calls_after_compact_uses_technical_user_not_orphan_output(
    monkeypatch,
):
    from processing_responses import process_tool_calls

    sys_m = {"role": "system", "content": "s", "id": "sys"}
    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "session_compact",
        "arguments": '{"reason": "phase done"}',
        "id": "fc1",
    }
    batch = [call]
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.messages = [sys_m, {"role": "user", "content": "long history", "id": "u0"}]
    state.session_compact_applied = False

    def fake_apply_tool(**kwargs):
        if kwargs["tool_name"] == "session_compact":
            state.messages = [sys_m, {"role": "user", "content": "summary", "id": "sum"}]
            state.session_compact_applied = True
            return json.dumps({"tool": "session_compact", "ok": True, "compacted": True})
        return "{}"

    monkeypatch.setattr("processing_responses.apply_tool", fake_apply_tool)
    monkeypatch.setattr(
        "processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )

    out = process_tool_calls(
        batch,
        state,
        ctx=SimpleNamespace(config_dict={}),
        run_agent_func=lambda *_a, **_k: None,
    )

    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "session_compact finished" in str(out[0].get("content") or "")
    assert not any(m.get("type") == "function_call_output" for m in out)
    assert state.session_compact_applied is False


def test_process_tool_calls_after_compact_keeps_other_tool_pairs(monkeypatch):
    from processing_responses import process_tool_calls

    sys_m = {"role": "system", "content": "s", "id": "sys"}
    compact_call = {
        "type": "function_call",
        "call_id": "c_compact",
        "name": "session_compact",
        "arguments": '{"reason": "done"}',
        "id": "fc_compact",
    }
    other_call = {
        "type": "function_call",
        "call_id": "c_other",
        "name": "session_think",
        "arguments": '{"thoughts": "x"}',
        "id": "fc_other",
    }
    batch = [compact_call, other_call]
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=2,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.messages = [sys_m, {"role": "user", "content": "history", "id": "u0"}]
    state.session_compact_applied = False

    def fake_apply_tool(**kwargs):
        if kwargs["tool_name"] == "session_compact":
            state.messages = [sys_m, {"role": "user", "content": "summary", "id": "sum"}]
            state.session_compact_applied = True
            return json.dumps({"tool": "session_compact", "ok": True})
        return json.dumps({"tool": "session_think", "ok": True})

    monkeypatch.setattr("processing_responses.apply_tool", fake_apply_tool)
    monkeypatch.setattr(
        "processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )

    out = process_tool_calls(
        batch,
        state,
        ctx=SimpleNamespace(config_dict={}),
        run_agent_func=lambda *_a, **_k: None,
    )

    call_ids = [
        m.get("call_id")
        for m in out
        if m.get("type") in ("function_call", "function_call_output")
    ]
    assert call_ids == ["c_other", "c_other"]
    assert out[-1]["role"] == "user"
    assert "session_compact finished" in str(out[-1].get("content") or "")
