"""Фаза 4: usage субагента сворачивается в категорию subagent родителя."""

import json
from types import SimpleNamespace

from gc_core.tools.subagents.runner import SubagentRunner
from gc_core.summarize.config import resolve_summary_limits
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
        self.usage.add(CATEGORY_MAIN, 100, 20)
        self.usage.add(CATEGORY_SUMMARY, 10, 2)


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
    )
    return SubagentRunner(ctx, orch, ctx.settings), orch


async def test_finally_merge_folds_child_usage_into_parent(monkeypatch):
    runner, orch = _runner_with_orchestrator()
    child = _FakeChildState()

    # Подменяем тяжёлые шаги: create_instance/build/run заменяем на лёгкие заглушки.
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
    # Сжатие у ребёнка выключено: пороги нулевые (координатор — no-op).
    monkeypatch.setattr(
        runner_mod,
        "resolve_summary_limits_at_level",
        lambda cfg, router, level: resolve_summary_limits(None),
    )

    async def _create(*a, **k):
        return _Inst()

    subagent_cls = SimpleNamespace(create_instance=_create)

    async def _fake_run_agent(messages, child_state, child_ctx, **kwargs):
        return "done"

    out = await runner._arun_internal(
        "subagent_worker", subagent_cls, {"task": "t"}, _fake_run_agent
    )
    payload = json.loads(out)

    assert payload["ok"] is True
    # Родитель получил вклад субагента в категорию subagent (100+10 in, 20+2 out)
    assert orch.usage.by_category["subagent"]["input"] == 110
    assert orch.usage.by_category["subagent"]["output"] == 22
    # Envelope содержит разбивку usage субагента
    assert "usage" in payload["meta"]


def test_child_judge_usage_is_merged_once_with_model_identity():
    runner, parent = _runner_with_orchestrator()
    child = _FakeChildState()
    child.usage.add("routing_judge", 40, 5, model="judge-model")
    runner._push_usage_delta(child)
    runner._push_usage_delta(child)
    assert parent.usage.by_category["subagent"]["calls"] == 3
    assert parent.usage.by_model["judge-model"]["input"] == 40
    assert parent.usage.by_model["judge-model"]["output"] == 5
    assert child.usage.by_category["routing_judge"]["calls"] == 1
