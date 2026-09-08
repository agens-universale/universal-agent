"""Невалидные аргументы одного вызова — output этого вызова, а не отказ всего батча.

``processing_responses`` импортируется при сборке (до стабов ``tools`` из других
тестов), а класс исключения берётся с этого же объекта модуля — тогда ``raise``
в фейковом apply_tool всегда совпадает с ``except`` в контроллере.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core import processing_responses as pr


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


def _ctx():
    return SimpleNamespace(config_dict={}, run_id="run", subagent_progress_sink=None, logger=None)


def _fc(name, call_id, arguments="{}"):
    return {"id": f"id_{call_id}", "type": "function_call", "call_id": call_id, "name": name, "arguments": arguments}


def _patch(monkeypatch, calls):
    def fake_apply_tool(**kw):
        calls.append(kw["tool_name"])
        if kw["tool_name"] == "session_think":
            raise pr.WrongFunctionArgumentsException(
                "Unexpected field 'bogus'", received_args=kw["arguments"], tool_name="session_think"
            )
        return '{"ok": true, "text": "worker done"}'

    monkeypatch.setattr(pr, "apply_tool", fake_apply_tool)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")


def test_invalid_call_does_not_drop_siblings(monkeypatch):
    calls: list[str] = []
    _patch(monkeypatch, calls)
    batch = [_fc("session_think", "c1", '{"bogus": 1}'), _fc("subagent_worker", "c2", '{"task": "t"}')]

    outs = pr.process_tool_calls(messages=batch, state=_state(), ctx=_ctx(), run_agent_func=None)

    assert calls == ["session_think", "subagent_worker"]
    assert [o["call_id"] for o in outs] == ["c1", "c2"]
    assert json.loads(outs[1]["output"]) == {"ok": True, "text": "worker done"}


def test_invalid_call_output_is_an_invalid_arguments_envelope(monkeypatch):
    _patch(monkeypatch, [])
    outs = pr.process_tool_calls(
        messages=[_fc("session_think", "c1", '{"bogus": 1}')], state=_state(), ctx=_ctx(), run_agent_func=None
    )
    payload = json.loads(outs[0]["output"])
    assert payload["ok"] is False
    assert payload["error"]["type"] == "invalid_arguments"
    assert payload["error"]["tool"] == "session_think"
    assert payload["error"]["message"] == "Unexpected field 'bogus'"
    assert "Wrong arguments for `session_think`" in payload["message"]
    assert '{"bogus": 1}' not in payload["message"]
    assert "You sent:" not in payload["message"]


def test_invalid_calls_are_counted_for_the_error_budget(monkeypatch):
    _patch(monkeypatch, [])
    state = _state()
    pr.process_tool_calls(
        messages=[_fc("session_think", "c1"), _fc("subagent_worker", "c2")], state=state, ctx=_ctx(), run_agent_func=None
    )
    assert state.last_batch_invalid_arguments_count == 1
    assert state.last_batch_unknown_tool_count == 0


def test_truncated_output_hint_when_finish_reason_is_length(monkeypatch):
    _patch(monkeypatch, [])
    outs = pr.process_tool_calls(
        messages=[_fc("session_think", "c1")], state=_state(last_finish_reason="length"), ctx=_ctx(), run_agent_func=None
    )
    assert "Output was truncated" in json.loads(outs[0]["output"])["message"]


def test_envelope_shape():
    exc = pr.WrongFunctionArgumentsException("bad args", received_args="{}", tool_name="fs_read_file")
    payload = json.loads(exc.to_tool_output())
    assert payload["ok"] is False
    assert payload["error"] == {"type": "invalid_arguments", "tool": "fs_read_file", "message": "bad args"}
    assert payload["message"].startswith("❌ Wrong arguments for `fs_read_file`: bad args")


def test_truncation_hint_is_appended_to_the_tool_hint_not_instead_of_it(monkeypatch):
    def fake_apply_tool(**kw):
        raise pr.WrongFunctionArgumentsException(
            "Unexpected field 'bogus'", received_args=kw["arguments"], tool_name="session_think",
            extra_hint="TOOL HINT: allowed fields are thought, next_step",
        )

    monkeypatch.setattr(pr, "apply_tool", fake_apply_tool)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    outs = pr.process_tool_calls(
        messages=[_fc("session_think", "c1")], state=_state(last_finish_reason="length"), ctx=_ctx(), run_agent_func=None
    )
    message = json.loads(outs[0]["output"])["message"]
    assert "TOOL HINT: allowed fields are thought, next_step" in message
    assert "Output was truncated" in message


_ASK_HUMAN_SPEC = {
    "name": "turn_ask_human",
    "parameters": {
        "type": "object",
        "properties": {"question": {"type": "string"}},
        "required": ["question"],
        "additionalProperties": False,
    },
}


def _ctx_with_ask_human_spec():
    ctx = _ctx()
    ctx.tools_data = {"turn_ask_human": {"spec": _ASK_HUMAN_SPEC, "tool_fabric": None, "injected_fields": []}}
    return ctx


def _patch_ask_human(monkeypatch, calls):
    def fake_apply_tool(**kw):
        calls.append(kw["tool_name"])
        if kw["tool_name"] == "turn_ask_human" and "question" not in kw["arguments"]:
            raise pr.WrongFunctionArgumentsException(
                "Missing required field 'question'", received_args=kw["arguments"], tool_name="turn_ask_human"
            )
        return '{"ok": true}'

    monkeypatch.setattr(pr, "apply_tool", fake_apply_tool)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **kw: res)
    monkeypatch.setattr(pr, "maybe_update_intent_scope_from_tool", lambda **kw: None)
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")


def test_a_finalizer_with_invalid_arguments_does_not_skip_its_siblings(monkeypatch):
    calls: list[str] = []
    _patch_ask_human(monkeypatch, calls)
    batch = [_fc("turn_ask_human", "c1", '{"bogus": 1}'), _fc("session_memory", "c2", '{"action": "read"}')]

    outs = pr.process_tool_calls(messages=batch, state=_state(), ctx=_ctx_with_ask_human_spec(), run_agent_func=None)

    assert calls == ["turn_ask_human", "session_memory"]
    assert json.loads(outs[0]["output"])["error"]["type"] == "invalid_arguments"
    assert json.loads(outs[1]["output"]) == {"ok": True}


def test_a_valid_ask_human_skips_later_work_siblings(monkeypatch):
    calls: list[str] = []
    _patch_ask_human(monkeypatch, calls)
    batch = [_fc("turn_ask_human", "c1", '{"question": "which one?"}'), _fc("session_memory", "c2", '{"action": "read"}')]

    outs = pr.process_tool_calls(messages=batch, state=_state(), ctx=_ctx_with_ask_human_spec(), run_agent_func=None)

    assert calls == ["turn_ask_human"]
    assert json.loads(outs[1]["output"])["skipped"] is True


def test_a_valid_ask_human_skips_turn_finish(monkeypatch):
    calls: list[str] = []
    _patch_ask_human(monkeypatch, calls)
    batch = [
        _fc("turn_ask_human", "c1", '{"question": "which one?"}'),
        _fc("turn_finish", "c2", '{"solution_type": "full_solution"}'),
    ]

    outs = pr.process_tool_calls(messages=batch, state=_state(), ctx=_ctx_with_ask_human_spec(), run_agent_func=None)

    assert calls == ["turn_ask_human"]
    assert json.loads(outs[1]["output"])["skipped"] is True


def test_an_invalid_ask_human_does_not_skip_turn_finish(monkeypatch):
    calls: list[str] = []
    _patch_ask_human(monkeypatch, calls)
    batch = [
        _fc("turn_ask_human", "c1", '{"bogus": 1}'),
        _fc("turn_finish", "c2", '{"solution_type": "full_solution"}'),
    ]

    outs = pr.process_tool_calls(messages=batch, state=_state(), ctx=_ctx_with_ask_human_spec(), run_agent_func=None)

    assert calls == ["turn_ask_human", "turn_finish"]
    assert json.loads(outs[0]["output"])["error"]["type"] == "invalid_arguments"
    assert json.loads(outs[1]["output"]) == {"ok": True}
