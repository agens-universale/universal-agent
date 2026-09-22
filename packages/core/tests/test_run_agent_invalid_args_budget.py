"""run_agent: невалидные аргументы уходят модели как function_call_output и тратят бюджет ошибок.

``processing_responses`` импортируется при сборке (до стабов ``tools`` из других
тестов), класс исключения берётся с этого же объекта модуля.
"""

from __future__ import annotations

import json

import pytest
from gc_core import processing_responses as pr
from gc_testkit import as_coro, run_coro


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
    cd = {
        "backend_api_mode": "chat_completions",
        "console_trace": False,
        "no_tool_call_max_count": 1,
        "strict_turn_finish_validation": True,
        "text_answer_mode": "allow",
    }
    return type("C", (), {"config_dict": cd, "logger": None})()


def _bad_batch(call_id):
    return [
        {
            "id": f"id_{call_id}",
            "type": "function_call",
            "call_id": call_id,
            "name": "session_think",
            "arguments": '{"bogus": 1}',
        }
    ]


def test_invalid_args_reach_the_model_as_tool_output_and_exhaust_the_budget(monkeypatch):
    received_by_model = []

    def _fake_user_ask(messages, state, ctx):
        return _bad_batch("c1"), "resp_1"

    def _fake_tool_outputs(messages, state, ctx):
        received_by_model.append(list(messages))
        return _bad_batch("c2"), "resp_2"

    def _apply_tool(**kw):
        raise pr.WrongFunctionArgumentsException(
            "Unexpected field 'bogus'", received_args=kw["arguments"], tool_name="session_think"
        )

    monkeypatch.setattr(pr, "aprocess_user_ask", as_coro(_fake_user_ask))
    monkeypatch.setattr(pr, "aprocess_tool_outputs", as_coro(_fake_tool_outputs))
    monkeypatch.setattr(pr, "aapply_tool", as_coro(_apply_tool))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    state = _state()
    result = run_coro(pr.arun_agent([{"role": "user", "content": "сделай", "id": "u1"}], state, _run_ctx()))

    assert result == "Agent tools call errors."
    assert state.terminal_outcome == "tool_errors"
    # Модель получила именно function_call_output на упавший вызов, а не user-сообщение.
    assert len(received_by_model) == 1
    outputs = [m for m in received_by_model[0] if m.get("type") == "function_call_output"]
    assert [m["call_id"] for m in outputs] == ["c1"]
    assert json.loads(outputs[0]["output"])["error"]["type"] == "invalid_arguments"


def _bad_batch_many(call_ids, name="session_think"):
    return [
        {"id": f"id_{cid}", "type": "function_call", "call_id": cid, "name": name, "arguments": '{"bogus": 1}'}
        for cid in call_ids
    ]


def _unknown_tool_payload(name):
    return json.dumps(
        {
            "ok": False,
            "error": {
                "type": "unknown_tool",
                "message": f"No tool named '{name}' on client.",
                "requested": name,
                "suggestions": [],
            },
            "message": f"Unknown tool '{name}'. Use only tools from the provided tool list.",
        }
    )


def _raise_invalid(**kw):
    raise pr.WrongFunctionArgumentsException(
        "Unexpected field 'bogus'", received_args=kw["arguments"], tool_name=kw["tool_name"]
    )


def _return_unknown(**kw):
    return _unknown_tool_payload(kw["tool_name"])


def _run_two_bad_turns(monkeypatch, apply_tool, tool_name):
    """Ход 1: батч из четырёх плохих вызовов; ход 2: ещё один. Лимит no_tool_call_max_count = 1."""
    received_by_model = []

    def _fake_user_ask(messages, state, ctx):
        return _bad_batch_many(["c1", "c2", "c3", "c4"], tool_name), "resp_1"

    def _fake_tool_outputs(messages, state, ctx):
        received_by_model.append(list(messages))
        return _bad_batch_many([f"d{len(received_by_model)}"], tool_name), "resp_next"

    monkeypatch.setattr(pr, "aprocess_user_ask", as_coro(_fake_user_ask))
    monkeypatch.setattr(pr, "aprocess_tool_outputs", as_coro(_fake_tool_outputs))
    monkeypatch.setattr(pr, "aapply_tool", as_coro(apply_tool))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    ctx = _run_ctx()
    ctx.config_dict["max_unknown_tool_repeats"] = 100  # повтор одного имени — отдельный сторож, не о нём
    state = _state()
    result = run_coro(pr.arun_agent([{"role": "user", "content": "сделай", "id": "u1"}], state, ctx))
    return result, state, received_by_model


@pytest.mark.parametrize(
    "apply_tool, tool_name",
    [
        pytest.param(_raise_invalid, "session_think", id="invalid_arguments"),
        pytest.param(_return_unknown, "phase1", id="unknown_tool"),
    ],
)
def test_one_bad_batch_costs_one_unit_of_the_error_budget(monkeypatch, apply_tool, tool_name):
    result, state, received_by_model = _run_two_bad_turns(monkeypatch, apply_tool, tool_name)

    # Четыре плохих вызова одним ходом не исчерпали лимит 1: модель получила все четыре output'а...
    assert len(received_by_model) == 1
    outputs = [m for m in received_by_model[0] if m.get("type") == "function_call_output"]
    assert [m["call_id"] for m in outputs] == ["c1", "c2", "c3", "c4"]
    # ...а второй плохой ход подряд — уже перебор.
    assert result == "Agent tools call errors."
    assert state.terminal_outcome == "tool_errors"


def test_run_agent_resets_a_stale_invalid_arguments_count_at_entry(monkeypatch):
    seen = []

    def _fake_user_ask(messages, state, ctx):
        seen.append(state.last_batch_invalid_arguments_count)
        return _bad_batch("c1"), "resp_1"

    def _fake_tool_outputs(messages, state, ctx):
        return _bad_batch("c2"), "resp_2"

    monkeypatch.setattr(pr, "aprocess_user_ask", as_coro(_fake_user_ask))
    monkeypatch.setattr(pr, "aprocess_tool_outputs", as_coro(_fake_tool_outputs))
    monkeypatch.setattr(pr, "aapply_tool", as_coro(_raise_invalid))
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **kw: None))

    state = _state()
    state.last_batch_invalid_arguments_count = 7  # хвост прошлого прогона
    run_coro(pr.arun_agent([{"role": "user", "content": "сделай", "id": "u1"}], state, _run_ctx()))

    assert seen == [0]
