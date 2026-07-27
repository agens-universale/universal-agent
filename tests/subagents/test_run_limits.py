"""Subagent token/tool-call limit: ok=false, finish_reason=token_limit|tool_call_limit."""
from __future__ import annotations

import json

import pytest

from tests.subagents.test_step_limit import _FakeChildState, _runner, _wire_runner


@pytest.mark.parametrize("finish_reason", ["token_limit", "tool_call_limit"])
def test_run_limit_returns_ok_false_not_completed(monkeypatch, finish_reason):
    runner, _orch = _runner()
    child = _FakeChildState(steps=5, max_steps=50, terminal_outcome=finish_reason)
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.terminal_outcome = finish_reason
        child_state.steps = 5
        return f"Agent stopped because of {finish_reason}."

    out = runner._run_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status"] == finish_reason
    assert payload["error"]["code"] == finish_reason
    assert payload["meta"]["finish_reason"] == finish_reason
    assert payload["meta"]["steps"] == 5
    assert payload["meta"]["child_run_id"] == "cr-step"
    assert "result" not in payload or payload.get("ok") is False
