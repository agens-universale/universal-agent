"""child_run_id в output meta (без path к логу)."""

from __future__ import annotations

import json
from types import SimpleNamespace

from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.summarize.config import resolve_summary_limits
from gc_core.usage.account import UsageAccount
from gc_testkit import as_coro
from gc_testkit.settings import fake_ctx


class _FakeChildState:
    def __init__(self):
        self.state_id = "child-state"
        self.steps = 2
        self.max_steps = 50
        self.shared = None
        self.backend_api_mode = "chat_completions"
        self.messages = []
        self.usage = UsageAccount()
        self.current_user_intent: list = []
        self.user_turn_seq = 0
        self.tool_usage_notice_cnt: dict = {}
        self.terminal_outcome = None


def _wire(monkeypatch, runner, child, *, run_id="run-abc"):
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
            lambda tool_name: SimpleNamespace(
                logger=None,
                run_id=run_id,
                logging_path="/tmp/should-not-leak/agent_subagent_worker_run-abc.log",
            )
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
        lambda cfg, router, level: resolve_summary_limits({}),
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
    return SubagentRunner(ctx, orch)


async def test_success_meta_includes_child_run_id_not_path(monkeypatch):
    runner = _runner()
    child = _FakeChildState()
    cls = _wire(monkeypatch, runner, child, run_id="child-guid-1")

    async def _fake_run_agent(*a, **k):
        return "done"

    out = await runner._arun_internal(
        "subagent_worker",
        cls,
        {"task": "t"},
        _fake_run_agent,
    )
    payload = json.loads(out)
    assert payload["ok"] is True
    assert payload["meta"]["child_run_id"] == "child-guid-1"
    assert "child_log_path" not in payload["meta"]
    assert "logging_path" not in payload["meta"]
    blob = json.dumps(payload)
    assert "/tmp/" not in blob
    assert ".log" not in blob


async def test_run_failed_error_includes_child_run_id(monkeypatch):
    runner = _runner()
    child = _FakeChildState()
    cls = _wire(monkeypatch, runner, child, run_id="child-guid-err")

    async def boom(*a, **k):
        raise RuntimeError("boom")

    out = await runner._arun_internal(
        "subagent_worker", cls, {"task": "t"}, boom
    )
    payload = json.loads(out)
    assert payload["ok"] is False
    assert payload["meta"]["child_run_id"] == "child-guid-err"
    assert "child_log_path" not in payload["meta"]


async def test_early_error_without_ctx_has_no_child_run_id():
    """До build child_ctx run_id ещё нет — meta без child_run_id."""
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
    runner = SubagentRunner(ctx, orch)

    async def _fake_run_agent(*a, **k):
        return None

    out = await runner.arun("subagent_no_such", {"task": "t"}, _fake_run_agent)
    payload = json.loads(out)
    assert payload["ok"] is False
    assert "child_run_id" not in payload["meta"]
