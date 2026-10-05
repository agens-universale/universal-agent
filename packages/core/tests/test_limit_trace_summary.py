"""Tests for limit trace summary envelope (spec 2026-08-01-limit-trace-summary-spec.md).

Mock LLM: no live calls. Validates envelope shape, LLM failure, raw text passthrough,
config kill-switch, and no working_memory injection into the prompt.
"""

import json
from types import SimpleNamespace

from gc_core.states import State
from gc_core.summarize import limit_trace_summary as lts
from gc_core.summarize import llm as summary_llm
from gc_testkit import run_coro


def _make_state(max_steps=2):
    return State(
        verbose=False,
        model="Test",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=max_steps,
        system_prompt_id="v1",
        use_server_subagents=False,
    )


def _make_ctx(**overrides):
    cfg = {
        "limit_trace_summary": True,
        "max_tool_calls": None,
        "session_token_limit": None,
    }
    cfg.update(overrides)
    return SimpleNamespace(
        config_dict=cfg,
        logger=None,
        auxiliary_llm=SimpleNamespace(
            max_tokens=100,
            completion_kwargs={},
        ),
    )


# ---------------------------------------------------------------------------
# Envelope shape
# ---------------------------------------------------------------------------


def test_step_limit_envelope_shape(monkeypatch):
    async def _fake_llm(**kw):
        return "## Intent\n\nDid stuff."

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _fake_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [
        {"role": "system", "content": "system prompt"},
        {"role": "user", "content": "hello"},
        {"role": "assistant", "content": "working..."},
    ]
    result = run_coro(
        lts.abuild_limit_envelope(state, "step_limit", _make_ctx())
    )
    env = json.loads(result)
    assert env["ok"] is False
    assert env["status"] == "step_limit"
    assert "step" in env["message"].lower()
    assert env["raw_trace_summary"] == "## Intent\n\nDid stuff."


def test_tool_call_limit_envelope_shape(monkeypatch):
    async def _fake_llm(**kw):
        return "summary text"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _fake_llm
    )
    state = _make_state(max_steps=10)
    state.tool_calls = 5
    state.messages = [{"role": "user", "content": "hi"}]
    result = run_coro(
        lts.abuild_limit_envelope(
            state, "tool_call_limit", _make_ctx(max_tool_calls=5)
        )
    )
    env = json.loads(result)
    assert env["ok"] is False
    assert env["status"] == "tool_call_limit"
    assert "5" in env["message"]
    assert env["raw_trace_summary"] == "summary text"


def test_token_limit_envelope_shape(monkeypatch):
    async def _fake_llm(**kw):
        return "token summary"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _fake_llm
    )
    state = _make_state(max_steps=10)
    state.messages = [{"role": "user", "content": "hi"}]
    result = run_coro(
        lts.abuild_limit_envelope(
            state, "token_limit", _make_ctx(session_token_limit=100)
        )
    )
    env = json.loads(result)
    assert env["ok"] is False
    assert env["status"] == "token_limit"
    assert "token" in env["message"].lower()
    # counters: spent/limit (spent may be 0 when usage is empty)
    assert "/100" in env["message"]
    assert env["raw_trace_summary"] == "token summary"


# ---------------------------------------------------------------------------
# LLM failure -> empty summary, envelope still returned
# ---------------------------------------------------------------------------


def test_llm_failure_returns_envelope_with_empty_summary(monkeypatch):
    async def _boom(**kw):
        raise RuntimeError("LLM down")

    monkeypatch.setattr(summary_llm, "arequest_chat_completion_text", _boom)
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [{"role": "user", "content": "hi"}]
    result = run_coro(
        lts.abuild_limit_envelope(state, "step_limit", _make_ctx())
    )
    env = json.loads(result)
    assert env["ok"] is False
    assert env["status"] == "step_limit"
    assert env["raw_trace_summary"] == ""
    assert env["message"]  # non-empty


# ---------------------------------------------------------------------------
# Raw non-JSON model text still in raw_trace_summary
# ---------------------------------------------------------------------------


def test_raw_non_markdown_text_still_in_summary(monkeypatch):
    async def _fake_llm(**kw):
        return "not json, just plain text"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _fake_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [{"role": "user", "content": "hi"}]
    result = run_coro(
        lts.abuild_limit_envelope(state, "step_limit", _make_ctx())
    )
    env = json.loads(result)
    assert env["raw_trace_summary"] == "not json, just plain text"


# ---------------------------------------------------------------------------
# Config kill-switch: limit_trace_summary=false -> empty summary, no LLM call
# ---------------------------------------------------------------------------


def test_kill_switch_disables_llm_call(monkeypatch):
    called = {"n": 0}

    async def _fake_llm(**kw):
        called["n"] += 1
        return "should not be called"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _fake_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [{"role": "user", "content": "hi"}]
    result = run_coro(
        lts.abuild_limit_envelope(
            state, "step_limit", _make_ctx(limit_trace_summary=False)
        )
    )
    env = json.loads(result)
    assert env["raw_trace_summary"] == ""
    assert called["n"] == 0  # LLM never invoked


# ---------------------------------------------------------------------------
# No working_memory / shared memory in the prompt
# ---------------------------------------------------------------------------


def test_no_working_memory_in_prompt(monkeypatch):
    captured = {}

    async def _capture_llm(**kw):
        captured["messages"] = kw.get("messages", [])
        return "summary"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _capture_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "do task"},
        {"role": "assistant", "content": "working..."},
    ]
    run_coro(lts.abuild_limit_envelope(state, "step_limit", _make_ctx()))
    # The user content sent to LLM should contain the trace but NOT
    # "working_memory" or "shared memory" dump keywords.
    user_content = captured["messages"][1]["content"]
    assert "working_memory" not in user_content.lower()
    assert "do task" in user_content  # trace content present
    assert "working..." in user_content


# ---------------------------------------------------------------------------
# System message is stripped from trace serialization
# ---------------------------------------------------------------------------


def test_system_message_not_in_trace(monkeypatch):
    captured = {}

    async def _capture_llm(**kw):
        captured["messages"] = kw.get("messages", [])
        return "summary"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _capture_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [
        {"role": "system", "content": "SECRET_SYSTEM_PROMPT"},
        {"role": "user", "content": "hello"},
    ]
    run_coro(lts.abuild_limit_envelope(state, "step_limit", _make_ctx()))
    user_content = captured["messages"][1]["content"]
    assert "SECRET_SYSTEM_PROMPT" not in user_content
    assert "hello" in user_content


# ---------------------------------------------------------------------------
# Tool calls and outputs are serialized in the trace (not lost)
# ---------------------------------------------------------------------------


def test_function_call_serialized_in_trace(monkeypatch):
    captured = {}

    async def _capture_llm(**kw):
        captured["messages"] = kw.get("messages", [])
        return "summary"

    monkeypatch.setattr(
        summary_llm, "arequest_chat_completion_text", _capture_llm
    )
    state = _make_state(max_steps=1)
    state.steps = 2
    state.messages = [
        {"role": "system", "content": "sys"},
        {"role": "user", "content": "list files"},
        {
            "type": "function_call",
            "name": "exec_command",
            "arguments": '{"command": "ls -la"}',
            "call_id": "call_abc",
            "id": "fc_1",
            "status": "completed",
        },
        {
            "type": "function_call_output",
            "call_id": "call_abc",
            "output": "file1.txt\nfile2.txt",
            "id": "fco_1",
        },
        {"role": "assistant", "content": "Found 2 files."},
    ]
    run_coro(lts.abuild_limit_envelope(state, "step_limit", _make_ctx()))
    user_content = captured["messages"][1]["content"]

    # Tool call name and arguments must be present
    assert "exec_command" in user_content
    assert "ls -la" in user_content
    # Tool output must be present
    assert "file1.txt" in user_content
    assert "file2.txt" in user_content
    # Assistant text still present
    assert "Found 2 files." in user_content


# ---------------------------------------------------------------------------
# Config wiring: flatten_app_config must propagate limit_trace_summary
# from AgentConfig into config_dict (regression guard for missing wiring).
# ---------------------------------------------------------------------------


def test_kill_switch_wired_from_config():
    """flatten_app_config must include limit_trace_summary from AgentConfig.

    Without this wiring, `agent.limit_trace_summary: false` in YAML has no
    effect — _should_enable always returns True (default on) because the
    key is absent from config_dict. Unit tests that set the key by hand
    on a SimpleNamespace mask the bug.
    """
    from gc_core.config import flatten_app_config
    from gc_core.config_modules.agent import AgentConfig
    from gc_core.config_modules.root import AppConfig

    # False case: kill-switch must reach config_dict as False
    app_cfg_off = AppConfig(agent=AgentConfig(limit_trace_summary=False))
    cd_off = flatten_app_config(app_cfg_off)
    assert cd_off["limit_trace_summary"] is False, (
        "flatten_app_config must wire agent.limit_trace_summary=False into config_dict"
    )

    # True case (default): key present and True
    app_cfg_on = AppConfig(agent=AgentConfig())
    cd_on = flatten_app_config(app_cfg_on)
    assert cd_on["limit_trace_summary"] is True, (
        "flatten_app_config must wire agent.limit_trace_summary=True (default) into config_dict"
    )


def test_trace_carries_no_attachment_metadata_or_image_bytes():
    """Сериализация трейса берёт content — ключ attachments (пути, размеры) в
    промпт аварийной сводки не попадает (спека §5)."""
    message = {
        "role": "user",
        "id": "m1",
        "content": "[Изображение #1] что тут?",
        "attachments": [
            {
                "id": "0000000000000001",
                "kind": "image",
                "path": "/secret/cli_t_1.png",
            }
        ],
    }
    assert (
        lts._serialize_trace([message]) == "[user] [Изображение #1] что тут?"
    )
