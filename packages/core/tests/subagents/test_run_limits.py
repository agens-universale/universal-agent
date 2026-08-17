"""Subagent token/tool-call limit: ok=false, finish_reason=token_limit|tool_call_limit."""
from __future__ import annotations

import json

import pytest

from subagents.test_step_limit import _FakeChildState, _runner, _wire_runner


@pytest.mark.parametrize("finish_reason", ["token_limit", "tool_call_limit"])
def test_run_limit_returns_ok_false_not_completed(monkeypatch, finish_reason):
    runner, _orch = _runner()
    child = _FakeChildState(steps=5, max_steps=50, terminal_outcome=finish_reason)
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.terminal_outcome = finish_reason
        child_state.steps = 5
        # Child run_agent returns a limit envelope (build_limit_envelope).
        return json.dumps({
            "ok": False,
            "status": finish_reason,
            "message": f"Agent stopped: {finish_reason}.",
            "raw_trace_summary": "",
        })

    out = runner._run_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    # Runner pass-through: child envelope returned as-is (spec §6.2).
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status"] == finish_reason
    assert finish_reason in payload["message"]
    assert "raw_trace_summary" in payload
    assert "error" not in payload  # no _limit_response rebuild
