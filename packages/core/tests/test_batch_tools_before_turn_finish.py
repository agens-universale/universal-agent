"""Батч с turn_finish: сначала вся work, потом финализация (SPEC §3)."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from gc_core import processing_responses as pr
from gc_core.intent_guard import guard as intent_guard_guard
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.loop_detector.constants import LOOP_WARN_KEY, READ_PROFILE
from gc_core.processing_responses import (
    _split_batch_new_around_finish,
    arun_agent,
    partition_work_then_finish,
)
from gc_core.states import State
from gc_core.tools.turn.submit_solution import SubmitSolution
from gc_testkit import run_coro
from gc_testkit.settings import fake_ctx


def _async_noop(fn):
    """Корутинная заглушка: ядро зовёт диспетчер и хуки только async-именами.

    Синхронную подмену цикл получил бы как готовый результат вместо awaitable
    и упал бы на ``await`` — или, хуже, тихо не исполнил бы её вовсе.
    """

    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)

    return _wrapped


def _state(**extra):
    return SimpleNamespace(
        messages=[],
        steps=1,
        max_steps=50,
        session_compact_applied=False,
        tool_calls=0,
        turn_tool_calls=0,
        **extra,
    )


def _ctx(tools_data=None):
    return fake_ctx(
        run_id="run",
        subagent_progress_sink=None,
        logger=None,
        tools_data=tools_data,
    )


def _fc(name, call_id, arguments="{}"):
    return {
        "id": f"id_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _finish_args(**overrides):
    payload = {
        "solution_type": "full_solution",
        "task_report": "Сделано по запросу пользователя",
        "handoff": "{}",
    }
    payload.update(overrides)
    return json.dumps(payload, ensure_ascii=False)


def _finish_spec():
    return SubmitSolution.tool_description()


def _patch_apply(monkeypatch, calls, *, results=None, fail_names=None):
    fail_names = set(fail_names or [])
    results = results or {}

    def fake_apply(**kw):
        name = kw["tool_name"]
        calls.append(name)
        if name in fail_names:
            raise pr.WrongFunctionArgumentsException(
                "bad args", received_args=kw["arguments"], tool_name=name
            )
        if name in results:
            return results[name]
        if name == "turn_finish":
            return json.dumps(
                {
                    "tool": "turn_finish",
                    "ok": True,
                    "status": "submitted",
                    "message": "Task outcome submitted.",
                    "error": None,
                    "task_report": "Сделано по запросу пользователя",
                },
                ensure_ascii=False,
            )
        if name == "session_memory":
            return json.dumps(
                {"tool": "session_memory", "ok": True, "action": "write"},
                ensure_ascii=False,
            )
        return json.dumps({"tool": name, "ok": True}, ensure_ascii=False)

    monkeypatch.setattr(pr, "aapply_tool", _async_noop(fake_apply))
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res
    )
    monkeypatch.setattr(
        pr,
        "amaybe_update_intent_scope_from_tool",
        _async_noop(lambda **kw: None),
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")


def _outputs_by_call(messages):
    return {
        m["call_id"]: m
        for m in messages
        if m.get("type") == "function_call_output"
    }


def _is_cycle_stop_stub(output: str) -> bool:
    return "skipped_on_cycle_stop" in output


# --- split: позиция finish не режет work ------------------------------------


def test_split_batch_new_keeps_notice_and_compact_after_finish():
    work = {
        "type": "function_call_output",
        "call_id": "c_work",
        "output": "{}",
    }
    finish = {
        "type": "function_call_output",
        "call_id": "c_fin",
        "output": "{}",
    }
    notice = {"role": "user", "content": "Loop detector: repeated"}
    resume = {"role": "user", "content": "session_compact finished."}
    prefix, fin, tail = _split_batch_new_around_finish(
        [work, finish, notice, resume], "c_fin"
    )
    assert prefix == [work]
    assert fin == [finish]
    assert tail == [notice, resume]


def test_split_batch_new_without_finish_is_all_prefix():
    work = {"type": "function_call_output", "call_id": "c_work"}
    notice = {"role": "user", "content": "Loop detector: repeated"}
    prefix, fin, tail = _split_batch_new_around_finish([work, notice], "c_fin")
    assert prefix == [work, notice]
    assert fin == []
    assert tail == []


def test_split_finish_does_not_drop_work():
    finish, memory = {"name": "turn_finish"}, {"name": "session_memory"}
    executed, skipped = partition_work_then_finish([finish, memory])
    assert executed == [memory, finish]
    assert skipped == []


def test_split_memory_then_finish_is_all_work_order():
    calls = [{"name": "session_memory"}, {"name": "turn_finish"}]
    executed, skipped = partition_work_then_finish(calls)
    assert executed == calls
    assert skipped == []


def test_split_ask_human_defers_runtime_barrier_decision():
    memory, ask, later, finish = (
        {"name": "session_memory"},
        {"name": "turn_ask_human"},
        {"name": "fs_read_file"},
        {"name": "turn_finish"},
    )
    executed, skipped = partition_work_then_finish(
        [memory, ask, later, finish]
    )
    assert executed == [memory, ask, later, finish]
    assert skipped == []


# --- process_tool_calls: work исполняется, не cycle-stop stub ---------------


def test_process_tool_calls_executes_memory_before_finish(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls)
    batch = [
        _fc("session_memory", "c_mem", '{"action":"write"}'),
        _fc("turn_finish", "c_fin", _finish_args()),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    by_call = _outputs_by_call(outs)
    assert calls == ["session_memory", "turn_finish"]
    assert set(by_call) == {"c_mem", "c_fin"}
    assert json.loads(by_call["c_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["c_mem"]["output"])
    assert "пропущено" not in by_call["c_mem"]["output"].lower()


def test_process_tool_calls_finish_first_still_runs_memory(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls)
    batch = [
        _fc("turn_finish", "c_fin", _finish_args()),
        _fc("session_memory", "c_mem", '{"action":"write"}'),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    by_call = _outputs_by_call(outs)
    assert calls == ["session_memory", "turn_finish"]
    assert json.loads(by_call["c_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["c_mem"]["output"])


def test_process_tool_calls_all_work_before_finish(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls)
    batch = [
        _fc("session_memory", "c1", '{"action":"write"}'),
        _fc("fs_read_file", "c2", '{"path":"a.py"}'),
        _fc("turn_finish", "c_fin", _finish_args()),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    assert calls == ["session_memory", "fs_read_file", "turn_finish"]
    by_call = _outputs_by_call(outs)
    assert set(by_call) == {"c1", "c2", "c_fin"}
    for cid in ("c1", "c2"):
        assert not _is_cycle_stop_stub(by_call[cid]["output"])


def test_process_tool_calls_second_finish_not_executed(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls)
    batch = [
        _fc("session_memory", "c_mem", '{"action":"write"}'),
        _fc("turn_finish", "c_fin1", _finish_args()),
        _fc("turn_finish", "c_fin2", _finish_args(task_report="второй")),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    assert calls == ["session_memory", "turn_finish"]
    by_call = _outputs_by_call(outs)
    assert set(by_call) == {"c_mem", "c_fin1", "c_fin2"}
    assert "skipped" in by_call["c_fin2"]["output"]
    assert not _is_cycle_stop_stub(by_call["c_mem"]["output"])


def test_process_tool_calls_work_error_does_not_drop_siblings(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls, fail_names={"session_memory"})
    batch = [
        _fc("session_memory", "c_mem", '{"bogus":1}'),
        _fc("fs_read_file", "c_read", '{"path":"a.py"}'),
        _fc("turn_finish", "c_fin", _finish_args()),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    assert calls == ["session_memory", "fs_read_file", "turn_finish"]
    by_call = _outputs_by_call(outs)
    assert json.loads(by_call["c_mem"]["output"])["ok"] is False
    assert json.loads(by_call["c_read"]["output"])["ok"] is True


def test_process_tool_calls_ask_human_is_work_and_blocks_finish(monkeypatch):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls)
    batch = [
        _fc("session_memory", "c_mem", '{"action":"write"}'),
        _fc("turn_ask_human", "c_ask", '{"question":"which?"}'),
        _fc("fs_read_file", "c_read", '{"path":"a.py"}'),
        _fc("turn_finish", "c_fin", _finish_args()),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    assert calls == ["session_memory", "turn_ask_human"]
    by_call = _outputs_by_call(outs)
    assert json.loads(by_call["c_mem"]["output"])["ok"] is True
    assert json.loads(by_call["c_read"]["output"])["skipped"] is True
    assert json.loads(by_call["c_fin"]["output"])["skipped"] is True


def test_process_tool_calls_rejected_ask_human_does_not_block_finish(
    monkeypatch,
):
    calls: list[str] = []
    rejected_ask = json.dumps(
        {
            "tool": "turn_ask_human",
            "ok": False,
            "error": {
                "type": "invalid_argument",
                "field": "question",
                "message": "question cannot be blank",
            },
            "question": " ",
            "response": "",
        },
        ensure_ascii=False,
    )
    _patch_apply(
        monkeypatch,
        calls,
        results={"turn_ask_human": rejected_ask},
    )
    batch = [
        _fc("turn_ask_human", "c_ask", '{"question":" "}'),
        _fc("turn_finish", "c_fin", _finish_args()),
    ]

    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )

    assert calls == ["turn_ask_human", "turn_finish"]
    by_call = _outputs_by_call(outs)
    assert json.loads(by_call["c_ask"]["output"])["ok"] is False
    assert json.loads(by_call["c_fin"]["output"])["ok"] is True


def test_process_tool_calls_invalid_first_finish_still_skips_second(
    monkeypatch,
):
    calls: list[str] = []
    _patch_apply(monkeypatch, calls, fail_names={"turn_finish"})
    batch = [
        _fc("session_memory", "c_mem", '{"action":"write"}'),
        _fc("turn_finish", "c_fin1", '{"bogus":1}'),
        _fc("turn_finish", "c_fin2", _finish_args()),
    ]
    outs = run_coro(
        pr.aprocess_tool_calls(batch, _state(), _ctx(), run_agent_func=None)
    )
    assert calls == ["session_memory", "turn_finish"]
    by_call = _outputs_by_call(outs)
    assert json.loads(by_call["c_mem"]["output"])["ok"] is True
    assert json.loads(by_call["c_fin1"]["output"])["ok"] is False
    assert "skipped" in by_call["c_fin2"]["output"]


# --- run_agent: work в трейсе до закрытия хода --------------------------------


def _agent_state(*, max_steps: int = 5):
    return State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _agent_ctx(**overrides):
    cfg = {
        "backend.api_mode": "chat_completions",
        "agent.strict_turn_finish_validation": False,
        "agent.no_tool_call_max_count": 5,
    }
    cfg.update(overrides)
    return fake_ctx(
        cfg,
        logger=None,
        auth_connector=None,
    )


def _patch_run_agent_apply(monkeypatch, applied, *, finish_output=None):
    def fake_apply(**kw):
        name = kw["tool_name"]
        applied.append(name)
        if name == "turn_finish":
            if finish_output is not None:
                return finish_output
            args = kw.get("arguments") or ""
            if "bogus" in str(args):
                raise pr.WrongFunctionArgumentsException(
                    "Unexpected field 'bogus'",
                    received_args=args,
                    tool_name="turn_finish",
                )
            return json.dumps(
                {
                    "tool": "turn_finish",
                    "ok": True,
                    "status": "submitted",
                    "message": "Task outcome submitted.",
                    "error": None,
                    "task_report": "Сделано по запросу пользователя",
                },
                ensure_ascii=False,
            )
        if name == "session_memory":
            return json.dumps(
                {
                    "tool": "session_memory",
                    "ok": True,
                    "action": "write",
                    "register": "knowledge",
                },
                ensure_ascii=False,
            )
        return json.dumps({"tool": name, "ok": True}, ensure_ascii=False)

    monkeypatch.setattr(pr, "aapply_tool", _async_noop(fake_apply))


def test_run_agent_accepted_finish_drops_loop_notice(monkeypatch):
    """Закрывающий finish не оставляет technical user началом нового хода."""
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    state = _agent_state()
    ctx = _agent_ctx()
    ctx.tools_data = {
        "fs_read_file": {
            LOOP_WARN_KEY: READ_PROFILE,
            "tool_fabric": None,
            "injected_fields": [],
        },
        "turn_finish": {"tool_fabric": None, "injected_fields": []},
    }
    persisted: list[dict] = []

    def persist(msgs):
        persisted.extend(msgs)

    args = '{"path":"a.py"}'
    finish = _fc("turn_finish", "call_finish", _finish_args())
    result = run_coro(
        arun_agent(
            [
                _fc("fs_read_file", "c1", args),
                _fc("fs_read_file", "c2", args),
                finish,
            ],
            state,
            ctx,
            persist_messages_callback=persist,
        )
    )
    assert result == finish["arguments"]

    notices = [
        m
        for m in state.messages
        if m.get("role") == "user"
        and "Loop detector:" in str(m.get("content", ""))
    ]
    assert notices == []
    by_call = _outputs_by_call(state.messages)
    assert by_call["c1"]["call_id"] == "c1"
    assert "call_finish" in by_call

    persisted_notices = [
        m
        for m in persisted
        if m.get("role") == "user"
        and "Loop detector:" in str(m.get("content", ""))
    ]
    assert persisted_notices == []


def test_run_agent_accepted_finish_drops_compact_resume(monkeypatch):
    """Compact-resume не переживает батч, который успешно закрыл ход."""
    _patch_run_agent_apply(monkeypatch, [])
    finish = _fc("turn_finish", "call_finish", _finish_args())
    finish_output = json.dumps(
        {
            "tool": "turn_finish",
            "ok": True,
            "status": "submitted",
            "task_report": "Сделано",
        },
        ensure_ascii=False,
    )
    resume = {"role": "user", "content": "session_compact finished. Continue."}

    async def _stub_process_tool_calls(*_args, **_kwargs):
        return [
            {
                "type": "function_call_output",
                "call_id": "call_finish",
                "output": finish_output,
            },
            resume,
        ]

    monkeypatch.setattr(pr, "aprocess_tool_calls", _stub_process_tool_calls)
    state = _agent_state()
    persisted: list[dict] = []

    result = run_coro(
        arun_agent(
            [finish],
            state,
            _agent_ctx(),
            persist_messages_callback=lambda msgs: persisted.extend(msgs),
        )
    )

    assert result == finish["arguments"]
    assert resume not in state.messages
    assert resume not in persisted


def test_run_agent_memory_then_finish_writes_memory_and_closes(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    state = _agent_state()
    finish = _fc("turn_finish", "call_finish", _finish_args())
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    result = run_coro(arun_agent([memory, finish], state, _agent_ctx()))
    assert result == finish["arguments"]
    by_call = _outputs_by_call(state.messages)
    assert "session_memory" in applied
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["call_mem"]["output"])
    assert by_call["call_finish"]["output"]
    assert applied.index("session_memory") < applied.index("turn_finish")


def test_run_agent_finish_then_memory_still_writes_memory(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    state = _agent_state()
    finish = _fc("turn_finish", "call_finish", _finish_args())
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    result = run_coro(arun_agent([finish, memory], state, _agent_ctx()))
    assert result == finish["arguments"]
    by_call = _outputs_by_call(state.messages)
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["call_mem"]["output"])
    assert applied.index("session_memory") < applied.index("turn_finish")


def test_run_agent_invalid_finish_keeps_work_and_stays_open(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after",
                }
            ],
            "rid",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    spec = _finish_spec()
    ctx = _agent_ctx(**{"agent.strict_turn_finish_validation": False})
    ctx.tools_data = {
        "turn_finish": {
            "spec": spec,
            "tool_fabric": None,
            "injected_fields": [],
        }
    }
    state = _agent_state(max_steps=0)
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    finish = _fc("turn_finish", "call_finish", '{"bogus": 1}')
    result = run_coro(arun_agent([memory, finish], state, ctx))
    assert result != finish["arguments"]
    by_call = _outputs_by_call(state.messages)
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    finish_payload = json.loads(by_call["call_finish"]["output"])
    assert finish_payload["ok"] is False
    assert "session_memory" in applied
    assert "turn_finish" in applied


@pytest.mark.parametrize("backend", ["chat_completions", "responses"])
def test_run_agent_rejected_finish_output_keeps_turn_open(
    monkeypatch, backend
):
    applied: list[str] = []
    rejected = json.dumps(
        {
            "tool": "turn_finish",
            "ok": False,
            "status": "rejected",
            "message": "Validation failed.",
            "error": {
                "type": "invalid_argument",
                "field": "solution_type",
                "message": "unsupported solution_type",
            },
            "task_report": "",
        },
        ensure_ascii=False,
    )
    _patch_run_agent_apply(monkeypatch, applied, finish_output=rejected)

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after_reject",
                }
            ],
            "rid_reject",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    state = _agent_state(max_steps=0)
    ctx = _agent_ctx(
        **{"backend.api_mode": backend},
        **{"agent.strict_turn_finish_validation": False},
    )
    ctx.tools_data = {
        "turn_finish": {
            "spec": _finish_spec(),
            "tool_fabric": None,
            "injected_fields": [],
        }
    }
    finish = _fc(
        "turn_finish",
        "call_finish",
        _finish_args(solution_type="unsupported"),
    )

    result = run_coro(arun_agent([finish], state, ctx))

    assert result != finish["arguments"]
    assert applied == ["turn_finish"]
    payload = json.loads(
        _outputs_by_call(state.messages)["call_finish"]["output"]
    )
    assert payload["ok"] is False
    assert payload["status"] == "rejected"
    assert payload["error"]["field"] == "solution_type"


def test_run_agent_gate_reject_keeps_work_in_trace(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    work_in_digest: list[bool] = []

    async def _reject(**_kwargs):
        work_in_digest.append(
            any(
                m.get("type") == "function_call_output"
                and m.get("call_id") == "call_mem"
                for m in state.messages
            )
        )
        return ValidationResult(
            ok=False,
            action="fix_report_and_continue",
            trace_ok=False,
            report_ok=False,
            trace_gaps=["m"],
            report_mismatches=["c"],
            missing_requirements=["m"],
            conflicts=["c"],
            coverage_score=0.0,
            feedback_for_agent="improve the answer",
        )

    monkeypatch.setattr(
        intent_guard_guard,
        "avalidate_final_report_vs_user_intent",
        _reject,
    )

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after_gate",
                }
            ],
            "rid_stub",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    state = _agent_state(max_steps=0)
    finish = _fc("turn_finish", "call_finish", _finish_args())
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    result = run_coro(
        arun_agent(
            [memory, finish],
            state,
            _agent_ctx(**{"agent.strict_turn_finish_validation": True}),
        )
    )
    assert result != finish["arguments"]
    assert work_in_digest == [True]
    by_call = _outputs_by_call(state.messages)
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["call_mem"]["output"])
    finish_payload = json.loads(by_call["call_finish"]["output"])
    assert finish_payload["status"] == "rejected_by_finish_gate"


def test_run_agent_successful_ask_keeps_turn_open(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "after ask"}],
                    "id": "m_after_ask",
                }
            ],
            "rid_ask",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    state = _agent_state(max_steps=0)
    ask = _fc("turn_ask_human", "call_ask", '{"question":"which?"}')
    finish = _fc("turn_finish", "call_finish", _finish_args())
    result = run_coro(arun_agent([ask, finish], state, _agent_ctx()))
    assert result != finish["arguments"]
    assert applied == ["turn_ask_human"]
    by_call = _outputs_by_call(state.messages)
    assert "skipped" in by_call["call_finish"]["output"]
    assert "generated before that reply" in by_call["call_finish"]["output"]


def test_run_agent_tool_rejected_finish_keeps_work_and_stays_open(monkeypatch):
    applied: list[str] = []
    rejected = json.dumps(
        {
            "tool": "turn_finish",
            "ok": False,
            "status": "rejected",
            "message": "Validation failed.",
            "error": {
                "type": "invalid_argument",
                "field": "solution_type",
                "message": "unsupported solution_type",
            },
            "task_report": "",
        },
        ensure_ascii=False,
    )
    _patch_run_agent_apply(monkeypatch, applied, finish_output=rejected)

    async def _stub_user_ask(_messages, _state, _ctx):
        return (
            [
                {
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "retry"}],
                    "id": "m_after_sem",
                }
            ],
            "rid_sem",
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", _stub_user_ask)
    state = _agent_state(max_steps=0)
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    finish = _fc(
        "turn_finish",
        "call_finish",
        _finish_args(solution_type="unsupported"),
    )
    result = run_coro(arun_agent([memory, finish], state, _agent_ctx()))
    assert result != finish["arguments"]
    assert applied == ["session_memory", "turn_finish"]
    by_call = _outputs_by_call(state.messages)
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    payload = json.loads(by_call["call_finish"]["output"])
    assert payload["ok"] is False
    assert payload["status"] == "rejected"


def test_run_agent_same_order_on_responses_backend(monkeypatch):
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    state = _agent_state()
    finish = _fc("turn_finish", "call_finish", _finish_args())
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')
    result = run_coro(
        arun_agent(
            [finish, memory],
            state,
            _agent_ctx(**{"backend.api_mode": "responses"}),
        )
    )
    assert result == finish["arguments"]
    by_call = _outputs_by_call(state.messages)
    assert json.loads(by_call["call_mem"]["output"])["ok"] is True
    assert not _is_cycle_stop_stub(by_call["call_mem"]["output"])
    assert applied.index("session_memory") < applied.index("turn_finish")


# --- R1: батч «work + finish» обязан ДОЕХАТЬ до истории треда ---------------


def _persisted_calls(persisted: list) -> set:
    """call_id всех function_call_output, которые дошли до персиста."""
    return {
        m.get("call_id")
        for m in persisted
        if isinstance(m, dict) and m.get("type") == "function_call_output"
    }


def test_finish_output_reaches_the_thread_on_responses_backend(monkeypatch):
    """Вывод ``turn_finish`` обязан доехать до хранилища. Замерено: тут он один.

    Три вызова ``_maybe_persist`` в этом пути пришли с merge develop, где
    функция была синхронной, и остались без ``await``: корутина создавалась и
    выбрасывалась. Два из трёх дублировали более ранний персист и потерь не
    давали — а третий, на ветке responses, оказался ЕДИНСТВЕННЫМ, кто пишет
    вывод ``turn_finish``. Проверено трассировкой всех вызовов: на этом пути
    ``call_finish`` персистится ровно здесь и больше нигде.

    Расхождение видно не сразу, а на ВОЗОБНОВЛЕНИИ треда: ``state.messages``
    наполнен, история — нет, и модель получает не то, что видела в прошлый раз.

    Проверяется наблюдаемое следствие, а не наличие ``await``.
    """
    applied: list[str] = []
    _patch_run_agent_apply(monkeypatch, applied)
    state = _agent_state()
    persisted: list = []

    finish = _fc("turn_finish", "call_finish", _finish_args())
    memory = _fc("session_memory", "call_mem", '{"action":"write"}')

    result = run_coro(
        arun_agent(
            [finish, memory],
            state,
            _agent_ctx(**{"backend.api_mode": "responses"}),
            persist_messages_callback=persisted.extend,
        )
    )

    assert result == finish["arguments"]
    got = _persisted_calls(persisted)
    assert "call_finish" in got, (
        f"вывод turn_finish не доехал до хранилища: {sorted(got)}"
    )


def test_nothing_in_state_is_missing_from_the_thread(monkeypatch):
    """Общее правило: что попало в ``state``, то попало и в хранилище.

    Ловит не конкретную забытую строку, а расхождение как класс — включая
    ветки, до которых сегодняшние сценарии не доходят.
    """
    for kw in ({}, {"backend.api_mode": "responses"}):
        applied: list[str] = []
        _patch_run_agent_apply(monkeypatch, applied)
        state = _agent_state()
        persisted: list = []

        finish = _fc("turn_finish", "call_finish", _finish_args())
        memory = _fc("session_memory", "call_mem", '{"action":"write"}')

        run_coro(
            arun_agent(
                [memory, finish],
                state,
                _agent_ctx(**kw),
                persist_messages_callback=persisted.extend,
            )
        )

        in_state = _persisted_calls(state.messages)
        in_store = _persisted_calls(persisted)
        assert in_state <= in_store, (
            f"{kw or 'chat_completions'}: в хранилище нет того, что уже в state: {sorted(in_state - in_store)}"
        )
