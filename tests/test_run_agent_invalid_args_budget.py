"""run_agent: невалидные аргументы уходят модели как function_call_output и тратят бюджет ошибок.

``processing_responses`` импортируется при сборке (до стабов ``tools`` из других
тестов), класс исключения берётся с этого же объекта модуля.
"""

from __future__ import annotations

import json

import processing_responses as pr


def _state():
    s = type("S", (), {})()
    s.messages = []
    s.steps = 0
    s.max_steps = 100
    s.tool_calls = 0
    s.turn_tool_calls = 0
    s.session_compact_applied = False
    s.current_user_intent = []
    s.user_intent_log = []
    s.flush_current_user_intent = lambda: None
    return s


def _run_ctx():
    cd = {"backend_api_mode": "chat_completions", "console_trace": False,
          "no_tool_call_max_count": 1, "strict_turn_finish_validation": True,
          "text_answer_mode": "allow"}
    return type("C", (), {"config_dict": cd, "logger": None})()


def _bad_batch(call_id):
    return [{"id": f"id_{call_id}", "type": "function_call", "call_id": call_id,
             "name": "session_think", "arguments": '{"bogus": 1}'}]


def test_invalid_args_reach_the_model_as_tool_output_and_exhaust_the_budget(monkeypatch):
    received_by_model = []

    def _fake_user_ask(messages, state, ctx):
        return _bad_batch("c1"), "resp_1"

    def _fake_tool_outputs(messages, state, ctx):
        received_by_model.append(list(messages))
        return _bad_batch("c2"), "resp_2"

    def _apply_tool(**kw):
        raise pr.WrongFunctionArgumentsException("Unexpected field 'bogus'", received_args=kw["arguments"],
                                                 tool_name="session_think")

    monkeypatch.setattr(pr, "process_user_ask", _fake_user_ask)
    monkeypatch.setattr(pr, "process_tool_outputs", _fake_tool_outputs)
    monkeypatch.setattr(pr, "apply_tool", _apply_tool)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)

    state = _state()
    result = pr.run_agent([{"role": "user", "content": "сделай", "id": "u1"}], state, _run_ctx())

    assert result == "Agent tools call errors."
    assert state.terminal_outcome == "tool_errors"
    # Модель получила именно function_call_output на упавший вызов, а не user-сообщение.
    assert len(received_by_model) == 1
    outputs = [m for m in received_by_model[0] if m.get("type") == "function_call_output"]
    assert [m["call_id"] for m in outputs] == ["c1"]
    assert json.loads(outputs[0]["output"])["error"]["type"] == "invalid_arguments"
