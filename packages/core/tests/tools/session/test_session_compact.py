import json
import types
from unittest.mock import MagicMock

from gc_core.states import State
from gc_core.tools.registry import get_builtin_tools_data
from gc_core.tools.session import session_compact as session_compact_module
from gc_core.tools.session.session_compact import (
    TRIGGER_REASON,
    SessionCompact,
)
from gc_testkit import run_coro
from gc_testkit.helpers.tool_ctx import CallableTool
from gc_testkit.settings import fake_ctx, make_settings


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
    assert "session_compact" not in get_builtin_tools_data(
        make_settings({"backend.api_mode": "responses"})
    )
    tools = get_builtin_tools_data(
        make_settings({"backend.api_mode": "chat_completions"})
    )
    assert "session_compact" in tools
    assert tools["session_compact"]["tool_fabric"] is SessionCompact


def test_session_compact_calls_arun_summary_compaction(monkeypatch):
    state = _state()
    ctx = fake_ctx(
        logger=None,
        on_summary_applied=MagicMock(),
        is_cli_mode=True,
    )
    captured = {}

    async def fake_compact(**kwargs):
        captured.update(kwargs)
        state.messages = [
            {"role": "system", "content": "sys"},
            {"role": "user", "content": "summary body", "id": "sum"},
        ]

    monkeypatch.setattr(
        session_compact_module, "arun_summary_compaction", fake_compact
    )

    tool = CallableTool(SessionCompact(ctx=ctx, state=state))
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
    res = json.loads(CallableTool(SessionCompact(state=_state()))(reason="  "))
    assert res["ok"] is False
    assert res["error"]["type"] == "invalid_argument"
    assert res["error"]["field"] == "reason"


def test_session_compact_rejects_responses_backend():
    res = json.loads(
        CallableTool(
            SessionCompact(state=_state(backend_api_mode="responses"))
        )(reason="phase done")
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "unsupported_backend"


def test_session_compact_rejects_empty_dialog():
    state = _state(messages=[{"role": "system", "content": "only sys"}])
    res = json.loads(
        CallableTool(SessionCompact(state=state))(reason="cleanup")
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "nothing_to_compact"


def test_session_compact_reports_compaction_failure(monkeypatch):
    state = _state()
    ctx = fake_ctx(logger=None)

    async def boom(**_kwargs):
        raise RuntimeError("llm down")

    monkeypatch.setattr(
        session_compact_module, "arun_summary_compaction", boom
    )

    res = json.loads(
        CallableTool(SessionCompact(ctx=ctx, state=state))(reason="retry")
    )
    assert res["ok"] is False
    assert res["error"]["type"] == "compaction_failed"
    assert "llm down" in res["error"]["message"]


def test_process_tool_calls_after_compact_uses_technical_user_not_orphan_output(
    monkeypatch,
):
    from gc_core.processing_responses import aprocess_tool_calls

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
    state.messages = [
        sys_m,
        {"role": "user", "content": "long history", "id": "u0"},
    ]
    state.session_compact_applied = False

    async def fake_apply_tool(**kwargs):
        if kwargs["tool_name"] == "session_compact":
            state.messages = [
                sys_m,
                {"role": "user", "content": "summary", "id": "sum"},
            ]
            state.session_compact_applied = True
            return json.dumps(
                {"tool": "session_compact", "ok": True, "compacted": True}
            )
        return "{}"

    monkeypatch.setattr(
        "gc_core.processing_responses.aapply_tool", fake_apply_tool
    )
    monkeypatch.setattr(
        "gc_core.processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )

    out = run_coro(
        aprocess_tool_calls(
            batch,
            state,
            ctx=fake_ctx(),
            run_agent_func=lambda *_a, **_k: None,
        )
    )

    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "session_compact finished" in str(out[0].get("content") or "")
    assert not any(m.get("type") == "function_call_output" for m in out)
    assert state.session_compact_applied is False


def test_after_compact_only_resume_and_summary_sees_closed_tail(monkeypatch):
    """Сводка видит только закрытые пары. Хвост батча не исполняется.
    Дальше уходит одно user-сообщение «продолжай»."""
    from gc_core.processing_responses import aprocess_tool_calls

    sys_m = {"role": "system", "content": "s", "id": "sys"}
    done_call = {
        "type": "function_call",
        "call_id": "c_done",
        "name": "session_think",
        "arguments": '{"thoughts": "done"}',
        "id": "fc_done",
    }
    compact_call = {
        "type": "function_call",
        "call_id": "c_compact",
        "name": "session_compact",
        "arguments": '{"reason": "done"}',
        "id": "fc_compact",
    }
    pending_call = {
        "type": "function_call",
        "call_id": "c_pending",
        "name": "session_think",
        "arguments": '{"thoughts": "later"}',
        "id": "fc_pending",
    }
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
    state.messages = [
        sys_m,
        {"role": "user", "content": "history", "id": "u0"},
        done_call,
        compact_call,
        pending_call,
    ]
    state.session_compact_applied = False
    called = []
    seen_ids = []
    logger = MagicMock()

    async def fake_apply_tool(**kwargs):
        called.append(kwargs["tool_name"])
        if kwargs["tool_name"] == "session_compact":
            seen_ids.extend(
                m.get("call_id")
                for m in state.messages
                if isinstance(m, dict)
                and m.get("type") in ("function_call", "function_call_output")
            )
            state.messages = [
                sys_m,
                {"role": "user", "content": "summary", "id": "sum"},
            ]
            state.session_compact_applied = True
            return json.dumps({"tool": "session_compact", "ok": True})
        return json.dumps({"tool": "session_think", "ok": True})

    monkeypatch.setattr(
        "gc_core.processing_responses.aapply_tool", fake_apply_tool
    )
    monkeypatch.setattr(
        "gc_core.processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )

    out = run_coro(
        aprocess_tool_calls(
            [done_call, compact_call, pending_call],
            state,
            ctx=fake_ctx(logger=logger),
            run_agent_func=lambda *_a, **_k: None,
        )
    )

    assert called == ["session_think", "session_compact"]
    assert seen_ids.count("c_done") == 2
    assert "c_pending" not in seen_ids
    assert "c_compact" not in seen_ids
    assert len(out) == 1
    assert out[0]["role"] == "user"
    assert "session_compact finished" in str(out[0].get("content") or "")
    assert "c_pending" not in str(out[0].get("content") or "")
    assert not any(
        m.get("type") in ("function_call", "function_call_output") for m in out
    )
    logger.warning.assert_called_once()
    logged = " ".join(str(part) for part in logger.warning.call_args.args)
    assert "c_pending" in logged
    assert "session_think" in logged


def test_failed_compact_keeps_the_rest_of_the_batch(monkeypatch):
    from gc_core.processing_responses import aprocess_tool_calls

    sys_m = {"role": "system", "content": "s", "id": "sys"}
    compact_call = {
        "type": "function_call",
        "call_id": "c_compact",
        "name": "session_compact",
        "arguments": '{"reason": "done"}',
        "id": "fc_compact",
    }
    later_call = {
        "type": "function_call",
        "call_id": "c_later",
        "name": "session_think",
        "arguments": '{"thoughts": "x"}',
        "id": "fc_later",
    }
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
    state.messages = [sys_m, compact_call, later_call]
    state.session_compact_applied = False
    called = []

    async def fake_apply_tool(**kwargs):
        called.append(kwargs["tool_name"])
        if kwargs["tool_name"] == "session_compact":
            return json.dumps(
                {
                    "tool": "session_compact",
                    "ok": False,
                    "error": {"type": "compaction_failed"},
                }
            )
        return json.dumps({"tool": "session_think", "ok": True})

    monkeypatch.setattr(
        "gc_core.processing_responses.aapply_tool", fake_apply_tool
    )
    monkeypatch.setattr(
        "gc_core.processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )

    out = run_coro(
        aprocess_tool_calls(
            [compact_call, later_call],
            state,
            ctx=fake_ctx(),
            run_agent_func=lambda *_a, **_k: None,
        )
    )

    assert called == ["session_compact", "session_think"]
    assert [m.get("call_id") for m in out] == ["c_compact", "c_later"]
    assert state.messages == [sys_m, compact_call, later_call]


def _compact_state():
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
    state.session_compact_applied = False
    return state


def _patch_tool_io(monkeypatch, apply):
    monkeypatch.setattr("gc_core.processing_responses.aapply_tool", apply)
    monkeypatch.setattr(
        "gc_core.processing_responses.maybe_store_and_wrap_tool_output",
        lambda tool_res, **_kw: tool_res,
    )


def test_successful_compact_marks_batch_trace(monkeypatch):
    """Контроллер батча пишет флаг в трейс, не в текст «продолжай»."""
    from gc_core.processing_responses import aprocess_tool_calls

    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "session_compact",
        "arguments": '{"reason": "phase done"}',
        "id": "fc1",
    }
    state = _compact_state()
    state.messages = [{"role": "system", "content": "s", "id": "sys"}, call]

    async def fake_apply_tool(**_kwargs):
        state.session_compact_applied = True
        return json.dumps(
            {"tool": "session_compact", "ok": True, "compacted": True}
        )

    _patch_tool_io(monkeypatch, fake_apply_tool)
    trace = {}
    run_coro(
        aprocess_tool_calls(
            [call],
            state,
            ctx=fake_ctx(),
            run_agent_func=lambda *_a, **_k: None,
            batch_trace=trace,
        )
    )

    assert trace.get("compacted") is True
    assert state.session_compact_applied is False


def test_resume_phrase_in_tool_output_does_not_mark_batch_trace(monkeypatch):
    from gc_core.execution.constants import _SESSION_COMPACT_RESUME_USER_TEXT
    from gc_core.processing_responses import aprocess_tool_calls

    call = {
        "type": "function_call",
        "call_id": "c1",
        "name": "session_think",
        "arguments": '{"thoughts": "x"}',
        "id": "fc1",
    }
    state = _compact_state()
    state.messages = [call]

    async def fake_apply_tool(**_kwargs):
        return json.dumps({"note": _SESSION_COMPACT_RESUME_USER_TEXT})

    _patch_tool_io(monkeypatch, fake_apply_tool)
    trace = {}
    run_coro(
        aprocess_tool_calls(
            [call],
            state,
            ctx=fake_ctx(),
            run_agent_func=lambda *_a, **_k: None,
            batch_trace=trace,
        )
    )

    assert trace.get("compacted") is not True


def _agent_loop(state, session_meta):
    from gc_core.processing_responses import _AgentLoop

    return _AgentLoop(
        state,
        fake_ctx(),
        get_full_history_messages=None,
        persist_messages_callback=None,
        pre_backend_compaction_hook=None,
        turn_end_compaction_hook=None,
        force_compact_hook=None,
        get_pending_messages=None,
        check_stop=None,
        session_meta=session_meta,
        mark_finalizing=None,
        mark_finish_gate_checking=None,
    )


def _function_call():
    return [
        {
            "type": "function_call",
            "call_id": "c1",
            "name": "session_think",
            "arguments": "{}",
            "id": "fc1",
        }
    ]


def test_controller_sets_compacted_from_batch_trace(monkeypatch):
    """Флаг наджа ставит контроллер по трейсу, даже если в ответе нет маркера."""
    from gc_core import processing_responses as pr

    state = _compact_state()
    state.tool_usage_notice_cnt = {"session_todo": 3}
    state.todo_reminder_cnt = 4
    session_meta = {"project_memory_check_cnt": 7}
    loop = _agent_loop(state, session_meta)
    loop._nudges.buffer.emit("hint")

    async def fake_aprocess(
        messages,
        state,
        ctx,
        run_agent_func,
        persist_messages_callback=None,
        batch_trace=None,
    ):
        del messages, state, ctx, run_agent_func, persist_messages_callback
        batch_trace["compacted"] = True
        return [{"role": "user", "content": "continue", "id": "resume"}]

    monkeypatch.setattr(pr, "aprocess_tool_calls", fake_aprocess)
    run_coro(loop._afunction_call(_function_call(), "chat_completions"))

    assert loop._nudges.compacted is True
    assert state.tool_usage_notice_cnt == {}
    assert state.todo_reminder_cnt == 0
    assert session_meta["project_memory_check_cnt"] == 0
    assert not loop._nudges.buffer


def test_resume_phrase_does_not_set_nudge_flag(monkeypatch):
    from gc_core import processing_responses as pr
    from gc_core.execution.constants import _SESSION_COMPACT_RESUME_USER_TEXT

    state = _compact_state()
    state.tool_usage_notice_cnt = {"session_todo": 3}
    state.todo_reminder_cnt = 4
    session_meta = {"project_memory_check_cnt": 7}
    loop = _agent_loop(state, session_meta)

    async def fake_aprocess(
        messages,
        state,
        ctx,
        run_agent_func,
        persist_messages_callback=None,
        batch_trace=None,
    ):
        del (
            messages,
            state,
            ctx,
            run_agent_func,
            persist_messages_callback,
            batch_trace,
        )
        return [
            {
                "role": "user",
                "content": _SESSION_COMPACT_RESUME_USER_TEXT,
                "id": "quoted",
            }
        ]

    monkeypatch.setattr(pr, "aprocess_tool_calls", fake_aprocess)
    run_coro(loop._afunction_call(_function_call(), "chat_completions"))

    assert loop._nudges.compacted is False
    assert state.tool_usage_notice_cnt == {"session_todo": 3}
    assert state.todo_reminder_cnt == 4
    assert session_meta["project_memory_check_cnt"] == 7
