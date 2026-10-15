"""Subagent step-limit: ok=false, finish_reason=step_limit (не completed)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.summarize.config import resolve_summary_limits
from gc_core.usage.account import UsageAccount
from gc_testkit import as_coro
from gc_testkit.settings import fake_ctx


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
        # Runner зовёт асинхронный хук порта: синхронный ушёл бы в поток и
        # оттуда в портал, а тот из-под работающего loop отказывает.
        async def afinalize_role(self, ctx, state):
            return None

        def get_initial_data(self):
            return "role", "assignment"

    monkeypatch.setattr(
        runner, "_build_child_state", as_coro(lambda role: child)
    )
    monkeypatch.setattr(
        runner,
        "_build_child_ctx",
        as_coro(
            lambda tool_name: SimpleNamespace(logger=None, run_id="cr-step")
        ),
    )
    monkeypatch.setattr(runner, "_build_initial_messages", lambda *a, **k: [])
    monkeypatch.setattr(runner, "_log_lifecycle", lambda **k: None)

    from gc_core.tools.subagents import runner as runner_mod

    monkeypatch.setattr(
        runner_mod, "init_subagent_open_turn", lambda *a, **k: None
    )
    monkeypatch.setattr(
        runner_mod,
        "get_thread_memory_registers_from_shared",
        lambda shared: {},
    )
    monkeypatch.setattr(
        runner_mod, "subagent_thread_memory_bootstrap_text", lambda cfg, mf: ""
    )
    # Сжатие у ребёнка выключено: пороги нулевые (координатор — no-op).
    monkeypatch.setattr(
        runner_mod,
        "resolve_summary_limits_at_level",
        lambda cfg, router, level: resolve_summary_limits(None),
    )

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
    ctx = fake_ctx(
        {"agent.use_local_subagents": True},
        logger=None,
        run_id="r",
        auth_connector=None,
        is_tui=False,
    )
    return SubagentRunner(ctx, orch), orch


async def test_step_limit_returns_ok_false_not_completed(monkeypatch):
    runner, _orch = _runner()
    child = _FakeChildState(
        steps=5, max_steps=5, terminal_outcome="step_limit"
    )
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.terminal_outcome = "step_limit"
        child_state.steps = 5
        # Child run_agent returns a limit envelope (build_limit_envelope).
        return json.dumps(
            {
                "ok": False,
                "status": "step_limit",
                "message": "Agent stopped: step limit (5/5).",
                "raw_trace_summary": "## Intent\n\nDid work.",
            }
        )

    out = await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    # Runner pass-through: child envelope returned as-is (spec §6.2).
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["status"] == "step_limit"
    assert "step" in payload["message"].lower()
    assert payload["raw_trace_summary"] == "## Intent\n\nDid work."
    assert "error" not in payload  # no _limit_response rebuild


async def test_normal_complete_still_ok_true(monkeypatch):
    runner, _orch = _runner()
    child = _FakeChildState(steps=2, max_steps=50, terminal_outcome=None)
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        return "done"

    out = await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["meta"]["finish_reason"] == "completed"


async def test_judge_unavailable_in_the_child_is_a_failure_for_the_parent(
    monkeypatch,
):
    """Проверка действий недоступна: субагент останавливается сам. Родителю
    приходит отказ, а не «успех» с текстом, и свой прогон он продолжает —
    причина остановки в его состояние не переносится."""
    runner, orch = _runner()
    child = _FakeChildState(steps=3, max_steps=50)
    subagent_cls = _wire_runner(monkeypatch, runner, child)
    reason = "The automatic review of actions is unavailable. Run stopped."

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.terminal_outcome = "judge_unavailable"
        return reason

    finished = []

    async def _finish(tool_name, child_state, child_ctx, outcome):
        finished.append(outcome)

    monkeypatch.setattr(runner, "_afinish_on_limit", _finish)
    out = await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)
    # завершение ребёнка записано как неуспешное (журнал, прогресс, трейс)
    assert finished == ["judge_unavailable"]
    assert payload["ok"] is False
    assert payload["error"]["code"] == "judge_unavailable"
    assert payload["error"]["message"] == reason
    assert payload["error"]["retryable"] is False
    assert getattr(orch, "judge_stop_reason", None) is None
    assert getattr(orch, "terminal_outcome", None) is None


def test_child_context_gets_the_root_users_words():
    """Судья действий ребёнка видит, что просил и что запретил человек в
    корневой сессии; задание родителя словами человека не является."""
    from gc_core.intent_log.record import IntentRecord

    runner, orch = _runner()
    orch.messages = [{"role": "user", "content": "собери отчёт"}]
    orch.user_intent_log = [
        IntentRecord(1, "ts", "user_ask", None, "ничего не пушь")
    ]
    orch.current_user_intent = []
    child_ctx = SimpleNamespace()
    runner._bind_child_context(child_ctx, "subagent_worker")
    assert child_ctx.root_user_messages() == ["ничего не пушь", "собери отчёт"]
    # читается в момент проверки: позже сказанное тоже попадёт
    orch.messages.append({"role": "user", "content": "и не трогай prod"})
    assert child_ctx.root_user_messages()[-1] == "и не трогай prod"


def test_nested_child_inherits_the_root_provider():
    runner, _orch = _runner()
    runner._ctx.root_user_messages = lambda: ["слова корня"]
    child_ctx = SimpleNamespace()
    runner._bind_child_context(child_ctx, "subagent_worker")
    assert child_ctx.root_user_messages() == ["слова корня"]
