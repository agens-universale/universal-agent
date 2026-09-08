"""Спека 04: unknown tool → JSON + suggestions; streak stop; tool_error_cnt."""
from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from gc_core.states import State
from gc_core.tools import aapply_tool, is_unknown_tool_output


def _async_noop(fn):
    """Заглушка корутинного хука: ядро зовёт async-двойник напрямую."""
    async def _wrapped(*args, **kwargs):
        return fn(*args, **kwargs)
    return _wrapped



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


def _registry(*names: str):
    return {
        name: {
            "tool_fabric": lambda **_kw: None,
            "spec": {"name": name, "parameters": {"type": "object", "properties": {}, "required": []}},
            "description": {"name": name},
            "injected_fields": [],
        }
        for name in names
    }


# --- A: apply_tool unknown JSON -------------------------------------------------


async def test_apply_tool_unknown_returns_json_with_suggestions():
    raw = await aapply_tool(
        tool_name="read_file",
        arguments="{}",
        tools_data_=_registry("fs_read_file", "fs_read_file_range", "session_think"),
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "unknown_tool"
    assert payload["error"]["requested"] == "read_file"
    assert "fs_read_file" in payload["error"]["suggestions"]
    assert "Maybe you mean tools:" in payload["message"]
    assert "fs_read_file" in payload["message"]
    assert is_unknown_tool_output(raw) is True


async def test_apply_tool_unknown_empty_suggestions_for_distant_name():
    raw = await aapply_tool(
        tool_name="zzzz_totally_unrelated_name_999",
        arguments="{}",
        tools_data_=_registry("session_think"),
    )
    payload = json.loads(raw)
    assert payload["ok"] is False
    assert payload["error"]["type"] == "unknown_tool"
    assert payload["error"]["suggestions"] == []
    assert "Maybe you mean tools:" not in payload["message"]
    assert "Use only tools from the provided tool list in section <tools> in system prompt." in payload["message"]


def test_is_unknown_tool_output_legacy_substring():
    assert is_unknown_tool_output("phase1 - there is not such tool on client") is True
    assert is_unknown_tool_output('{"ok": true, "message": "ok"}') is False


# --- B: streak in process_tool_calls --------------------------------------------


def test_process_tool_calls_updates_unknown_streak(monkeypatch):
    from gc_core import processing_responses as pr

    async def fake_apply(**kw):
        return json.dumps(
            {
                "ok": False,
                "error": {
                    "type": "unknown_tool",
                    "message": "No tool named 'phase1' on client.",
                    "requested": kw["tool_name"],
                    "suggestions": [],
                },
                "message": "Unknown tool 'phase1'. Use only tools from the provided tool list.",
            }
        )

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    state = _make_state()
    ctx = SimpleNamespace(config_dict={}, logger=None)
    messages = [
        {"type": "function_call", "name": "phase1", "call_id": "c1", "arguments": "{}"},
    ]
    outs = pr.process_tool_calls(messages, state, ctx, run_agent_func=None)
    assert len(outs) == 1
    assert state.last_unknown_tool_name == "phase1"
    assert state.unknown_tool_repeat_count == 1
    assert state.last_batch_unknown_tool_count == 1

    pr.process_tool_calls(messages, state, ctx, run_agent_func=None)
    assert state.unknown_tool_repeat_count == 2

    pr.process_tool_calls(messages, state, ctx, run_agent_func=None)
    assert state.unknown_tool_repeat_count == 3


def test_process_tool_calls_existing_tool_resets_streak(monkeypatch):
    from gc_core import processing_responses as pr

    async def fake_apply(**kw):
        if kw["tool_name"] == "phase1":
            return json.dumps(
                {
                    "ok": False,
                    "error": {
                        "type": "unknown_tool",
                        "message": "No tool named 'phase1' on client.",
                        "requested": "phase1",
                        "suggestions": [],
                    },
                    "message": "Unknown tool 'phase1'. Use only tools from the provided tool list.",
                }
            )
        return json.dumps({"ok": True, "message": "done"})

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **_kw: None))
    monkeypatch.setattr(pr, "generate_message_id_func", lambda p: f"{p}_x")

    state = _make_state()
    state.last_unknown_tool_name = "phase1"
    state.unknown_tool_repeat_count = 2
    ctx = SimpleNamespace(config_dict={}, logger=None)
    messages = [
        {"type": "function_call", "name": "phase1", "call_id": "c1", "arguments": "{}"},
        {"type": "function_call", "name": "session_think", "call_id": "c2", "arguments": "{}"},
    ]
    pr.process_tool_calls(messages, state, ctx, run_agent_func=None)
    assert state.last_unknown_tool_name is None
    assert state.unknown_tool_repeat_count == 0
    assert state.last_batch_unknown_tool_count == 1


# --- B: run_agent stop ----------------------------------------------------------


def test_run_agent_stops_after_unknown_tool_streak(monkeypatch):
    from gc_core import processing_responses as pr

    unknown_payload = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "unknown_tool",
                "message": "No tool named 'phase1' on client.",
                "requested": "phase1",
                "suggestions": [],
            },
            "message": "Unknown tool 'phase1'. Use only tools from the provided tool list.",
        }
    )

    async def fake_apply(**_kw):
        return unknown_payload

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **_kw: None))

    round_id = {"n": 0}

    async def fake_process_tool_outputs(messages, state, ctx):
        round_id["n"] += 1
        return (
            [
                {
                    "type": "function_call",
                    "name": "phase1",
                    "call_id": f"c{round_id['n']}",
                    "arguments": "{}",
                }
            ],
            f"resp_{round_id['n']}",
        )

    monkeypatch.setattr(pr, "aprocess_tool_outputs", fake_process_tool_outputs)

    state = _make_state(max_steps=50)
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "console_trace": False,
            "no_tool_call_max_count": 50,
            "max_unknown_tool_repeats": 5,
            "strict_turn_finish_validation": False,
        },
        logger=None,
    )
    messages = [
        {
            "type": "function_call",
            "name": "phase1",
            "call_id": "c0",
            "arguments": "{}",
        }
    ]
    result = pr.run_agent(messages, state, ctx)
    assert state.terminal_outcome == "tool_errors"
    assert "phase1" in result
    assert "repeated unknown tool" in result.lower()
    assert state.unknown_tool_repeat_count >= 5


def test_run_agent_unknown_increments_tool_error_without_reset(monkeypatch):
    """С высоким K unknown копит tool_error_cnt и упирается в no_tool_call_max_count."""
    from gc_core import processing_responses as pr

    unknown_payload = json.dumps(
        {
            "ok": False,
            "error": {
                "type": "unknown_tool",
                "message": "No tool named 'phase1' on client.",
                "requested": "phase1",
                "suggestions": [],
            },
            "message": "Unknown tool 'phase1'. Use only tools from the provided tool list.",
        }
    )

    async def fake_apply(**_kw):
        return unknown_payload

    monkeypatch.setattr(pr, "aapply_tool", fake_apply)
    monkeypatch.setattr(pr, "maybe_store_and_wrap_tool_output", lambda res, **_kw: res)
    monkeypatch.setattr(pr, "amaybe_update_intent_scope_from_tool", _async_noop(lambda **_kw: None))

    round_id = {"n": 0}

    async def fake_process_tool_outputs(messages, state, ctx):
        round_id["n"] += 1
        return (
            [
                {
                    "type": "function_call",
                    "name": "phase1",
                    "call_id": f"c{round_id['n']}",
                    "arguments": "{}",
                }
            ],
            f"resp_{round_id['n']}",
        )

    monkeypatch.setattr(pr, "aprocess_tool_outputs", fake_process_tool_outputs)

    state = _make_state(max_steps=50)
    ctx = SimpleNamespace(
        config_dict={
            "backend_api_mode": "chat_completions",
            "console_trace": False,
            "no_tool_call_max_count": 2,
            "max_unknown_tool_repeats": 100,
            "strict_turn_finish_validation": False,
        },
        logger=None,
    )
    messages = [
        {
            "type": "function_call",
            "name": "phase1",
            "call_id": "c0",
            "arguments": "{}",
        }
    ]
    result = pr.run_agent(messages, state, ctx)
    assert state.terminal_outcome == "tool_errors"
    assert "tool" in result.lower()
    # Если бы сбрасывали cnt в 0 после каждого unknown-батча — дошли бы до max_steps.
    assert state.steps <= 10


def test_agent_config_has_max_unknown_tool_repeats_default():
    from gc_core.config_modules.agent import AgentConfig

    assert AgentConfig().max_unknown_tool_repeats == 5
