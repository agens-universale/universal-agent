"""Подсказки хода живут в output инструмента, а не в отдельном user.

Приёмка спеки: loop, replay до маркера, accept/reject, битый вызов,
reasoning в отрендеренном запросе.
"""

from __future__ import annotations

import json
import logging

from gc_core import processing_responses as pr
from gc_core.auto_routing.policy import LOOP
from gc_core.intent_guard import guard as intent_guard_guard
from gc_core.intent_guard.text_answer_judge import TextAnswerDecision
from gc_core.intent_guard.validation_result import ValidationResult
from gc_core.loop_detector.constants import LOOP_WARN_KEY, READ_PROFILE
from gc_core.message_policy.chat_completions import (
    prepare_chat_completion_messages_for_api,
)
from gc_core.states import State
from gc_core.utils.format_messages import (
    format_message_chat_completions_to_send_backend,
)
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx
from helpers.turn_finish_apply import stub_turn_finish_apply_sync

_LOG = logging.getLogger("test_output_feedback_loop")


def _state() -> State:
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=6,
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    state.backend_api_mode = "chat_completions"
    state.messages = [
        {
            "id": "m_think",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "смотрю файл"}],
            "reasoning_content": "сначала прочитаю a.py",
        }
    ]
    return state


def _ctx(**overrides):
    cfg = {
        "backend.api_mode": "chat_completions",
        "logging.console_trace": False,
        "agent.no_tool_call_max_count": 5,
        "agent.strict_turn_finish_validation": False,
    }
    cfg.update(overrides)
    return fake_ctx(cfg, tools_data=_tools(), logger=_LOG, auth_connector=None)


def _tools() -> dict:
    return {
        "fs_read_file": {LOOP_WARN_KEY: READ_PROFILE},
        "turn_finish": {},
    }


def _fc(
    call_id: str,
    name: str = "fs_read_file",
    arguments: str = '{"path": "a.py"}',
) -> dict:
    return {
        "id": f"fc_{call_id}",
        "type": "function_call",
        "call_id": call_id,
        "name": name,
        "arguments": arguments,
    }


def _finish() -> dict:
    return _fc(
        "call_finish",
        "turn_finish",
        json.dumps(
            {
                "solution_type": "full_solution",
                "task_report": "done",
                "handoff": "{}",
            }
        ),
    )


def _patch_tools(monkeypatch) -> None:
    async def _apply(**kw):
        if kw["tool_name"] == "turn_finish":
            return stub_turn_finish_apply_sync(**kw)
        arguments = kw.get("arguments") or ""
        try:
            json.loads(arguments)
        except Exception as exc:
            raise pr.WrongFunctionArgumentsException(
                f"некорректный JSON: {exc}",
                received_args=arguments,
                tool_name=kw["tool_name"],
                expected_schema={
                    "name": kw["tool_name"],
                    "parameters": {
                        "type": "object",
                        "properties": {"path": {"type": "string"}},
                        "required": ["path"],
                    },
                },
            )
        return json.dumps({"ok": True, "content": "print(1)"})

    monkeypatch.setattr(pr, "aapply_tool", _apply)
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )


def _render(messages: list) -> list:
    formatted = [
        format_message_chat_completions_to_send_backend(message)
        for message in messages
    ]
    return prepare_chat_completion_messages_for_api(formatted)


def _harness(output: str) -> list:
    body = json.loads(output)
    return list(body.get("technical_system_messages") or [])


def test_continuing_turn_puts_loop_into_output_and_keeps_reasoning(
    monkeypatch,
):
    _patch_tools(monkeypatch)
    persisted: list = []
    rendered: list = []

    async def _outputs(messages, state, _ctx):
        rendered.append(_render(state.messages))
        return [_finish()], "resp_2"

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _outputs)
    monkeypatch.setattr(pr, "aprocess_user_ask", _outputs)
    state = _state()
    result = run_coro(
        pr.arun_agent(
            [_fc("c1"), _fc("c2")],
            state,
            _ctx(),
            persist_messages_callback=lambda msgs: persisted.extend(msgs),
        )
    )

    assert result == _finish()["arguments"]
    assert LOOP in state.tick_signals
    users = [
        message
        for message in state.messages
        if message.get("role") == "user"
        and "Loop detector:" in str(message.get("content", ""))
    ]
    assert users == []
    outputs = [
        message
        for message in state.messages
        if message.get("type") == "function_call_output"
        and message.get("call_id") == "c2"
    ]
    assert len(outputs) == 1
    hints = _harness(outputs[0]["output"])
    assert any("Loop detector:" in line for line in hints)
    assert json.loads(outputs[0]["output"])["ok"] is True

    first_c2 = next(
        message
        for message in persisted
        if message.get("type") == "function_call_output"
        and message.get("call_id") == "c2"
    )
    assert any(
        "Loop detector:" in line for line in _harness(first_c2["output"])
    )

    prompt = rendered[0]
    reasoning = [
        message.get("reasoning_content")
        for message in prompt
        if message.get("role") == "assistant"
    ]
    assert "сначала прочитаю a.py" in reasoning
    assert any(
        message.get("role") == "tool"
        and "technical_system_messages" in str(message.get("content"))
        for message in prompt
    )
    assert not any(
        message.get("role") == "user"
        and "Loop detector:" in str(message.get("content", ""))
        for message in prompt
    )


def test_accepted_finish_does_not_write_continuation(monkeypatch):
    _patch_tools(monkeypatch)
    state = _state()
    finish = _finish()
    result = run_coro(
        pr.arun_agent([_fc("c1"), _fc("c2"), finish], state, _ctx())
    )
    assert result == finish["arguments"]
    assert LOOP not in state.tick_signals
    for message in state.messages:
        if message.get("type") != "function_call_output":
            continue
        assert "technical_system_messages" not in str(message.get("output"))
        assert "Loop detector:" not in str(message.get("output"))


def test_rejected_finish_gets_loop_once_and_keeps_gate_fields(monkeypatch):
    _patch_tools(monkeypatch)

    async def _reject(**_kwargs):
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

    async def _accept_text(*_args, **_kwargs):
        return TextAnswerDecision(accept=True)

    monkeypatch.setattr(pr, "ashould_return_text_answer", _accept_text)

    async def _after_reject(_messages, _state, _ctx):
        return (
            [
                {
                    "id": "m_after",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "ещё работаю"}],
                }
            ],
            "resp_after",
        )

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _after_reject)
    state = _state()
    finish = _finish()
    result = run_coro(
        pr.arun_agent(
            [_fc("c1"), _fc("c2"), finish],
            state,
            _ctx(**{"agent.strict_turn_finish_validation": True}),
        )
    )
    assert result == "ещё работаю"
    assert LOOP in state.tick_signals
    finish_output = next(
        message
        for message in state.messages
        if message.get("type") == "function_call_output"
        and message.get("call_id") == "call_finish"
    )
    body = json.loads(finish_output["output"])
    assert body["ok"] is False
    assert body["status"] == "rejected_by_finish_gate"
    assert "improve" in body["message"] or body["message"]
    assert "Loop detector:" not in body["message"]
    assert any(
        "Loop detector:" in line for line in body["technical_system_messages"]
    )
    assert (
        sum(
            1
            for message in state.messages
            if message.get("role") == "user"
            and "Loop detector:" in str(message.get("content", ""))
        )
        == 0
    )


def test_after_session_compact_resets_notice_counters_in_flow(monkeypatch):
    """После саммари цикл флоу сбрасывает счётчики: для модели новый трейс."""
    from gc_core.message_policy.tool_usage_notice.state import (
        META_PROJECT_MEMORY_CHECK_CNT,
        META_TODO_REMINDER_CNT,
        META_TOOL_USAGE_NOTICE_CNT,
    )

    _patch_tools(monkeypatch)
    state = _state()
    state.tool_usage_notice_cnt = {"session_memory": 9}
    state.todo_reminder_cnt = 4
    state.mode_announced = "default"
    state.project_memory_content_hash = "keep-baseline"
    notice_meta = {
        META_TOOL_USAGE_NOTICE_CNT: {"session_memory": 9},
        META_TODO_REMINDER_CNT: 4,
        META_PROJECT_MEMORY_CHECK_CNT: 7,
    }
    sys_m = {"role": "system", "content": "s", "id": "sys"}
    state.messages = [
        sys_m,
        {
            "id": "m_think",
            "type": "message",
            "role": "assistant",
            "content": [{"type": "text", "text": "смотрю"}],
            "reasoning_content": "сначала сводка",
        },
    ]

    async def _apply(**kw):
        if kw["tool_name"] == "session_compact":
            state.messages = [
                sys_m,
                {"role": "user", "content": "summary", "id": "sum"},
            ]
            state.session_compact_applied = True
            return json.dumps(
                {"tool": "session_compact", "ok": True, "compacted": True}
            )
        return json.dumps({"ok": True, "content": "print(1)"})

    async def _after_compact(messages, _state, _ctx):
        return [_finish()], "resp_2"

    monkeypatch.setattr(pr, "aapply_tool", _apply)
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )
    monkeypatch.setattr(pr, "aprocess_tool_outputs", _after_compact)
    monkeypatch.setattr(pr, "aprocess_user_ask", _after_compact)

    ctx = _ctx()
    ctx.tools_data = {**_tools(), "session_compact": {}}
    run_coro(
        pr.arun_agent(
            [
                _fc("c1"),
                _fc(
                    "c_compact",
                    name="session_compact",
                    arguments='{"reason": "phase done"}',
                ),
            ],
            state,
            ctx,
            session_meta=notice_meta,
        )
    )
    assert state.tool_usage_notice_cnt == {}
    assert state.todo_reminder_cnt == 0
    assert notice_meta[META_TOOL_USAGE_NOTICE_CNT] == {}
    assert notice_meta[META_TODO_REMINDER_CNT] == 0
    assert notice_meta[META_PROJECT_MEMORY_CHECK_CNT] == 0
    assert state.mode_announced == "default"
    assert state.project_memory_content_hash == "keep-baseline"
    assert state.loop_detector_window == []
    assert state.loop_detector_pending == []


def test_broken_json_gets_own_failed_output_beside_a_live_one(monkeypatch):
    """Битый JSON — свой ok:false со схемой; к живому соседу harness не клеим."""
    _patch_tools(monkeypatch)
    seen: list = []

    async def _outputs(messages, _state, _ctx):
        seen.append(list(messages))
        return [_finish()], "resp_2"

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _outputs)
    monkeypatch.setattr(pr, "aprocess_user_ask", _outputs)
    run_coro(
        pr.arun_agent(
            [_fc("c1"), _fc("bad", arguments="{")],
            _state(),
            _ctx(),
        )
    )
    assert seen
    by_call = {
        message["call_id"]: message
        for message in seen[0]
        if message.get("type") == "function_call_output"
    }
    assert set(by_call) == {"c1", "bad"}
    live = json.loads(by_call["c1"]["output"])
    assert live["ok"] is True
    assert "technical_system_messages" not in live
    failed = json.loads(by_call["bad"]["output"])
    assert failed["ok"] is False
    assert failed["error"]["type"] == "invalid_arguments"
    assert "некорректный JSON" in failed["message"]
    assert "Correct call format" in failed["message"]
    assert '"path"' in failed["message"]
    assert not any(message.get("role") == "user" for message in seen[0])


def test_only_broken_json_still_gets_own_failed_output(monkeypatch):
    """Один битый JSON — тоже свой failed output, не user-отскок."""
    _patch_tools(monkeypatch)
    seen: list = []

    async def _ask(messages, _state, _ctx):
        seen.append(list(messages))
        return (
            [
                {
                    "id": "m_after",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "уточни"}],
                }
            ],
            "resp_ask",
        )

    async def _accept_text(*_args, **_kwargs):
        return TextAnswerDecision(accept=True)

    monkeypatch.setattr(pr, "aprocess_tool_outputs", _ask)
    monkeypatch.setattr(pr, "aprocess_user_ask", _ask)
    monkeypatch.setattr(pr, "ashould_return_text_answer", _accept_text)
    run_coro(pr.arun_agent([_fc("bad", arguments="{")], _state(), _ctx()))
    assert seen
    outputs = [
        message
        for message in seen[0]
        if message.get("type") == "function_call_output"
    ]
    assert len(outputs) == 1
    assert outputs[0]["call_id"] == "bad"
    body = json.loads(outputs[0]["output"])
    assert body["ok"] is False
    assert body["error"]["type"] == "invalid_arguments"
    assert "некорректный JSON" in body["message"]
    assert "Correct call format" in body["message"]
    assert '"path"' in body["message"]
    assert not any(
        message.get("role") == "user"
        and "некорректный JSON" in str(message.get("content", ""))
        for message in seen[0]
    )
