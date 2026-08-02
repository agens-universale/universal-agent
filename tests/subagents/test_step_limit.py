"""Subagent step-limit: ok=false, finish_reason=step_limit (не completed)."""
from __future__ import annotations

import json
from types import SimpleNamespace

from tools.subagents.runner import SubagentRunner
from usage.account import UsageAccount


class _FakeChildState:
    def __init__(self, *, steps=5, max_steps=5, terminal_outcome=None):
        self.state_id = "child-state"
        self.steps = steps
        self.max_steps = max_steps
        self.shared = None
        self.backend_api_mode = "chat_completions"
        self.messages = []
        self.usage = UsageAccount()
        self.current_user_intent: list = []
        self.user_turn_seq = 0
        self.tool_usage_notice_cnt: dict = {}
        self.terminal_outcome = terminal_outcome


def _wire_runner(monkeypatch, runner, child):
    class _Inst:
        def get_initial_data(self):
            return "role", "assignment"

    monkeypatch.setattr(runner, "_build_child_state", lambda role: child)
    monkeypatch.setattr(
        runner,
        "_build_child_ctx",
        lambda tool_name: SimpleNamespace(logger=None, run_id="cr-step"),
    )
    monkeypatch.setattr(runner, "_build_initial_messages", lambda *a, **k: [])
    monkeypatch.setattr(runner, "_log_lifecycle", lambda **k: None)

    import tools.subagents.runner as runner_mod
    monkeypatch.setattr(runner_mod, "init_subagent_open_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_mod, "get_thread_memory_registers_from_shared", lambda shared: {}
    )
    monkeypatch.setattr(
        runner_mod, "subagent_thread_memory_bootstrap_text", lambda cfg, mf: ""
    )
    monkeypatch.setattr(runner_mod, "parse_summary_trigger_chars", lambda cfg: 0)

    async def _create(*a, **k):
        return _Inst()

    return SimpleNamespace(create_instance=_create)


def _runner():
    orch = SimpleNamespace(
        usage=UsageAccount(),
        default_model="m",
        stop_requested=False,
        state_id="orch",
        discard_writes_after_stop=False,
    )
    ctx = SimpleNamespace(logger=None, run_id="r", auth_connector=None, is_tui=False)
    return SubagentRunner(ctx, orch, {"use_local_subagents": True}), orch


def test_step_limit_returns_ok_false_not_completed(monkeypatch):
    runner, _orch = _runner()
    child = _FakeChildState(steps=5, max_steps=5, terminal_outcome="step_limit")
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.terminal_outcome = "step_limit"
        child_state.steps = 5
        # Child run_agent returns a limit envelope (build_limit_envelope).
        return json.dumps({
            "ok": False,
            "status": "step_limit",
            "message": "Agent stopped: step limit (5/5).",
            "raw_trace_summary": "## Intent\n\nDid work.",
        })

    out = runner._run_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    # Runner pass-through: child envelope returned as-is (spec §6.2).
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status"] == "step_limit"
    assert "step" in payload["message"].lower()
    assert payload["raw_trace_summary"] == "## Intent\n\nDid work."
    assert "error" not in payload  # no _limit_response rebuild


def test_normal_complete_still_ok_true(monkeypatch):
    runner, _orch = _runner()
    child = _FakeChildState(steps=2, max_steps=50, terminal_outcome=None)
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        return "done"

    out = runner._run_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["meta"]["finish_reason"] == "completed"


def test_step_limit_trace_summary_not_success():
    from tui.services.trace_mapper import _looks_like_error, _summarize_output

    out = json.dumps(
        {
            "tool": "subagent_worker",
            "ok": False,
            "status": "step_limit",
            "message": "Subagent hit step limit.",
            "error": {"code": "step_limit", "message": "Subagent hit step limit.", "retryable": True},
            "meta": {"finish_reason": "step_limit", "steps": 50},
        }
    )
    assert _looks_like_error(out) is True
    assert _summarize_output(out, error=True) == "лимит шагов"
