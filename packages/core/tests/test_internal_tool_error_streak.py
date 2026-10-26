"""Предохранитель повторов для internal_tool_error.

`is_internal_tool_error_output` был экспортирован и не вызывался нигде в проде,
тогда как брат-близнец `is_unknown_tool_output` уже вёл счётчик повторов и
останавливал прогон. Из-за этого тул, падающий одинаково на каждом шаге, жёг
весь бюджет шагов: счётчик tool_error_cnt обнуляется, когда неизвестных тулов в
батче нет.
"""

from __future__ import annotations

import json

from gc_core.states import State
from gc_core.tools import is_internal_tool_error_output
from gc_testkit import as_coro, run_coro
from gc_testkit.settings import fake_ctx


def _make_state(**overrides):
    state = State(
        verbose=False,
        model="CodeChat",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=overrides.pop("max_steps", 50),
        system_prompt_id="v1",
        use_server_subagents=False,
    )
    for key, value in overrides.items():
        setattr(state, key, value)
    return state


_INTERNAL_ERROR = json.dumps(
    {
        "ok": False,
        "error": "internal_tool_error",
        "exception_type": "TypeError",
        "message": "unexpected keyword argument 'reason'",
        "guidance": "Client runtime failed while executing the tool.",
        "tool": "fs_read_file",
    },
    ensure_ascii=False,
)


def _patch(monkeypatch, output):
    from gc_core import processing_responses as pr

    monkeypatch.setattr(pr, "aapply_tool", as_coro(lambda **_kw: output))
    monkeypatch.setattr(
        pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res
    )
    monkeypatch.setattr(
        pr, "amaybe_update_intent_scope_from_tool", as_coro(lambda **_kw: None)
    )
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")
    return pr


def _call(pr, state, name="fs_read_file"):
    ctx = fake_ctx({}, logger=None)
    messages = [
        {
            "type": "function_call",
            "name": name,
            "call_id": "c1",
            "arguments": "{}",
        },
    ]
    return run_coro(
        pr.aprocess_tool_calls(messages, state, ctx, run_agent_func=None)
    )


def test_detector_recognises_the_envelope():
    assert is_internal_tool_error_output(_INTERNAL_ERROR) is True


def test_repeated_internal_error_from_one_tool_builds_a_streak(monkeypatch):
    pr = _patch(monkeypatch, _INTERNAL_ERROR)
    state = _make_state()

    _call(pr, state)
    assert state.last_internal_error_tool_name == "fs_read_file"
    assert state.internal_error_repeat_count == 1

    _call(pr, state)
    assert state.internal_error_repeat_count == 2

    _call(pr, state)
    assert state.internal_error_repeat_count == 3


def test_another_tool_restarts_the_streak(monkeypatch):
    pr = _patch(monkeypatch, _INTERNAL_ERROR)
    state = _make_state()

    _call(pr, state, name="fs_read_file")
    _call(pr, state, name="fs_read_file")
    assert state.internal_error_repeat_count == 2

    _call(pr, state, name="fs_list_files")
    assert state.last_internal_error_tool_name == "fs_list_files"
    assert state.internal_error_repeat_count == 1


def test_successful_call_resets_the_streak(monkeypatch):
    pr = _patch(monkeypatch, _INTERNAL_ERROR)
    state = _make_state()
    _call(pr, state)
    assert state.internal_error_repeat_count == 1

    _patch(monkeypatch, json.dumps({"ok": True, "tool": "fs_read_file"}))
    _call(pr, state)

    assert state.internal_error_repeat_count == 0
    assert state.last_internal_error_tool_name is None
