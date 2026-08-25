"""Bootstrap памяти треда шлётся ребёнку только если session_memory у него есть.

Текст директивный («update persistent registers with the thread memory tool»),
и приходит он на первом же шаге. Конфиг в этой точке ещё родительский, поэтому
доступность берётся из ``child_state.tools`` — он собран раньше и уже с учётом
сужения набора (файл сабагента, глобальный stop_tools, harbor).
"""
from types import SimpleNamespace

import pytest

import tools.subagents.runner as runner_mod
from tools.subagents.runner import SubagentRunner
from usage.account import UsageAccount

BOOT_MARKER = "BOOTSTRAP-MARKER"


class _FakeChildState:
    def __init__(self, tools):
        self.state_id = "child-state"
        self.steps = 1
        self.shared = None
        self.backend_api_mode = "chat_completions"
        self.messages = []
        self.tools = tools
        self.usage = UsageAccount()


def _runner():
    orch = SimpleNamespace(
        usage=UsageAccount(),
        default_model="m",
        stop_requested=False,
        state_id="orch",
        discard_writes_after_stop=False,
    )
    ctx = SimpleNamespace(logger=None, run_id="r", auth_connector=None)
    return SubagentRunner(ctx, orch, {"use_local_subagents": True})


def _run_with_tools(monkeypatch, tools):
    runner = _runner()
    child = _FakeChildState(tools)

    class _Inst:
        def finalize_role(self, child_ctx, orchestrator_state):
            # Волна профилей зовёт её ДО get_initial_data (роль спец-агента может
            # генерироваться LLM на контексте ребёнка) — фейк обязан её иметь,
            # иначе рана нет вовсе и гейт bootstrap проверять не на чем.
            return None

        def get_initial_data(self):
            return "role", "assignment"

    monkeypatch.setattr(runner, "_build_child_state", lambda role: child)
    monkeypatch.setattr(
        runner, "_build_child_ctx", lambda tool_name: SimpleNamespace(logger=None, run_id="cr")
    )
    monkeypatch.setattr(runner, "_build_initial_messages", lambda *a, **k: [])
    monkeypatch.setattr(runner, "_log_lifecycle", lambda **k: None)
    monkeypatch.setattr(runner_mod, "init_subagent_open_turn", lambda *a, **k: None)
    monkeypatch.setattr(
        runner_mod, "get_thread_memory_registers_from_shared", lambda shared: {}
    )
    monkeypatch.setattr(
        runner_mod, "subagent_thread_memory_bootstrap_text", lambda cfg, mf: BOOT_MARKER
    )
    monkeypatch.setattr(runner_mod, "parse_summary_trigger_chars", lambda cfg: 0)

    async def _create(*a, **k):
        return _Inst()

    runner._run_internal(
        "subagent_worker",
        SimpleNamespace(create_instance=_create),
        {"task": "t"},
        lambda messages, child_state, child_ctx, **kwargs: "done",
    )
    return child


@pytest.mark.parametrize(
    "tools, expected",
    [
        ([{"name": "session_memory"}, {"name": "fs_read_file"}], True),
        ([{"name": "fs_read_file"}, {"name": "turn_finish"}], False),
    ],
    ids=["tool_present", "tool_absent"],
)
def test_bootstrap_follows_tool_availability(monkeypatch, tools, expected):
    child = _run_with_tools(monkeypatch, tools)
    sent = any(BOOT_MARKER in str(m.get("content", "")) for m in child.messages)
    assert sent is expected


def test_unknown_tool_set_keeps_old_behaviour(monkeypatch):
    # Пустой список схем = «не знаем набор» (см. контракт None в tool_availability):
    # молча выключать bootstrap из-за незнания нельзя.
    child = _run_with_tools(monkeypatch, [])
    assert any(BOOT_MARKER in str(m.get("content", "")) for m in child.messages)
