"""Live usage: incremental child→parent merge, no double-count in finally."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.usage.account import CATEGORY_MAIN, CATEGORY_SUMMARY, UsageAccount
from gc_testkit import as_coro
from gc_testkit.settings import fake_ctx


class _FakeChildState:
    def __init__(self):
        self.state_id = "child-state"
        self.steps = 3
        self.shared = None
        self.backend_api_mode = "chat_completions"
        self.messages = []
        self.usage = UsageAccount()
        # Как после init_subagent_open_turn: list, не None (append_user_asks делает list(current)).
        self.current_user_intent: list = []
        self.user_turn_seq = 0
        self.tool_usage_notice_cnt: dict = {}


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
        as_coro(lambda tool_name: SimpleNamespace(logger=None, run_id="cr")),
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
    monkeypatch.setattr(
        runner_mod, "parse_summary_trigger_chars", lambda cfg: 0
    )

    async def _create(*a, **k):
        return _Inst()

    return SimpleNamespace(create_instance=_create)


def _runner_with_orchestrator():
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


async def test_live_delta_then_finally_no_double_count(monkeypatch):
    runner, orch = _runner_with_orchestrator()
    child = _FakeChildState()
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        persist = kwargs.get("persist_messages_callback")
        # step 1
        child_state.usage.add(CATEGORY_MAIN, 100, 20)
        if persist:
            persist([])
        assert orch.usage.by_category["subagent"]["input"] == 100
        assert orch.usage.by_category["subagent"]["output"] == 20
        # step 2
        child_state.usage.add(CATEGORY_SUMMARY, 10, 2)
        if persist:
            persist([])
        assert orch.usage.by_category["subagent"]["input"] == 110
        assert orch.usage.by_category["subagent"]["output"] == 22
        return "done"

    out = await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    # finally не удваивает уже запушенные дельты
    assert orch.usage.by_category["subagent"]["input"] == 110
    assert orch.usage.by_category["subagent"]["output"] == 22
    assert "usage" in payload["meta"]


async def test_finally_pushes_residual_if_no_mid_run_persist(monkeypatch):
    """Если persist не звали — finally всё равно догоняет residual delta."""
    runner, orch = _runner_with_orchestrator()
    child = _FakeChildState()
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.usage.add(CATEGORY_MAIN, 50, 5)
        return "done"

    await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    assert orch.usage.by_category["subagent"]["input"] == 50
    assert orch.usage.by_category["subagent"]["output"] == 5


async def test_parent_messages_untouched_by_usage_push(monkeypatch):
    """Контекст-бар родителя — только parent messages; runner не пишет в них."""
    runner, orch = _runner_with_orchestrator()
    orch.messages = [{"role": "user", "content": "parent only"}]
    child = _FakeChildState()
    child.messages = [{"role": "user", "content": "huge " * 5000}]
    subagent_cls = _wire_runner(monkeypatch, runner, child)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        child_state.usage.add(CATEGORY_MAIN, 1, 1)
        persist = kwargs.get("persist_messages_callback")
        if persist:
            persist([{"role": "assistant", "content": "x"}])
        return "done"

    await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    assert orch.messages == [{"role": "user", "content": "parent only"}]
