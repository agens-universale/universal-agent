"""Ожидание человека вычитается из часов хода (P1b, Task 2).

Точки ожидания ядра: вопрос агента (`turn_ask_human`), подтверждение через
порт рантайма и подтверждение локальным интерфейсом. Часы едут на `ctx`, как
`turn_id`: ожидания субагента ложатся на ход родителя.
"""

import asyncio
from types import SimpleNamespace

import pytest
from gc_core.agent_executor import AgentExecutor
from gc_core.config_modules import env_vars
from gc_core.execution.human import HumanRequest
from gc_core.execution.turn_clock import (
    TurnClock,
    bind_turn_clock,
    current_turn_clock,
    human_wait,
)
from gc_core.processing_responses import begin_turn_main_trace
from gc_core.security.confirm_port import (
    aconfirm_via_human_port,
    confirmation_step,
)
from gc_core.security.permission_store import PermissionStore
from gc_core.states import SharedStateData, State
from gc_core.tools.turn.ask_human import _aread_human_response
from gc_testkit.settings import fake_ctx

_THINK = 5.0


class _Mono:
    def __init__(self) -> None:
        self.now = 100.0

    def __call__(self) -> float:
        return self.now


def _clocked(**attrs):
    """Контекст с часами хода; время двигает тест."""
    mono = _Mono()
    ops = SimpleNamespace(monotonic=mono)
    ctx = fake_ctx(logger=None, operations=ops, **attrs)
    clock = TurnClock.start(mono=mono, wall=_wall)
    bind_turn_clock(ctx, clock)
    return ctx, clock, mono


def _wall():
    from datetime import UTC, datetime

    return datetime(2026, 10, 22, tzinfo=UTC)


def _state(**kw) -> State:
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="p",
        use_server_subagents=False,
        chat_completions_system_prompt="s",
        shared=SharedStateData(),
        **kw,
    )
    state.backend_api_mode = "chat_completions"
    return state


# --- human_wait ----------------------------------------------------------


def test_human_wait_without_clock_is_noop():
    with human_wait(SimpleNamespace()):
        pass


def test_human_wait_closes_on_exception():
    ctx, clock, mono = _clocked()
    with pytest.raises(KeyboardInterrupt):
        with human_wait(ctx):
            mono.now += _THINK
            raise KeyboardInterrupt
    mono.now += 1
    assert clock.elapsed() == 1


# --- вопрос агента -------------------------------------------------------


async def test_ask_human_port_wait_is_subtracted():
    mono_box = {}

    def port(_question):
        mono_box["mono"].now += _THINK
        return "да"

    ctx, clock, mono = _clocked(ask_human=port)
    mono_box["mono"] = mono
    assert await _aread_human_response(ctx, "?") == "да"
    assert clock.elapsed() == 0


async def test_human_request_port_wait_is_subtracted():
    ctx, clock, mono = _clocked()

    async def native(_request):
        mono.now += _THINK
        return "да"

    ctx.human_request = native
    assert await _aread_human_response(ctx, "?") == "да"
    assert clock.elapsed() == 0


async def test_stdin_wait_is_subtracted(monkeypatch):
    ctx, clock, mono = _clocked(ask_human=None)

    def read():
        mono.now += _THINK
        return "да"

    import gc_core.tools.turn.ask_human as ask_human

    monkeypatch.setattr(ask_human, "_read_stdin_lines", read)
    assert await _aread_human_response(ctx, "?") == "да"
    assert clock.elapsed() == 0


async def test_cancelled_ask_human_still_closes_wait():
    """Стоп снимает чтение ответа отменой: ожидание всё равно закрыто."""
    ctx, clock, mono = _clocked()

    async def port(_question):
        mono.now += _THINK
        raise asyncio.CancelledError

    ctx.ask_human = port
    with pytest.raises(asyncio.CancelledError):
        await _aread_human_response(ctx, "?")
    mono.now += 2
    assert clock.elapsed() == 2


# --- подтверждения -------------------------------------------------------


def test_tui_confirm_wait_is_subtracted():
    mono_box = {}

    def banner(**_kw):
        mono_box["mono"].now += _THINK
        return "allow_once"

    ctx, clock, mono = _clocked(is_tui=True, tui_confirm=banner)
    mono_box["mono"] = mono
    step = confirmation_step(title="t", question="q", fields=[], ctx=ctx)
    assert step.fn() is True
    assert clock.elapsed() == 0


async def test_human_request_confirm_wait_is_subtracted():
    ctx, clock, mono = _clocked()

    async def native(_request):
        mono.now += _THINK
        return "allow_once"

    ctx.human_request = native
    step = confirmation_step(title="t", question="q", fields=[], ctx=ctx)
    assert await step.make_coro() is True
    assert clock.elapsed() == 0


async def test_remembered_decision_is_not_a_wait():
    ctx, clock, mono = _clocked()
    store = PermissionStore()
    store.record("write:/x", True, session=True)
    ctx.permission_store = store

    async def native(_request):
        raise AssertionError("запомненное решение порт не спрашивает")

    ctx.human_request = native

    request = HumanRequest(
        kind="confirm", question="q", decision_key="write:/x"
    )
    mono.now += 3
    assert await aconfirm_via_human_port(request, ctx=ctx) is True
    assert clock.elapsed() == 3


# --- субагент ------------------------------------------------------------


def _parent_ctx(tmp_path, monkeypatch):
    from gc_testkit.helpers.tool_ctx import local_ctx

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    parent = local_ctx(
        {
            "agent.use_local_subagents": True,
            "backend.api_mode": "chat_completions",
            "paths.skills_data_path": "",
            "agent.model": "m",
            "agent.system_prompt_id": "p",
            "launch_root": str(launch_root),
        }
    )
    parent.run_id = "p1"
    parent.auth_connector = None
    parent.logger = None
    parent.logging_path = ""
    parent.summary_trace_path = ""
    parent.tools_data = {}
    parent.tools_spec = []
    parent.tools_description = []
    parent.subagent_progress_sink = None
    return parent


async def _child_ctx(parent):
    from gc_core.tools.subagents.runner import SubagentRunner

    runner = SubagentRunner(parent, _state(), parent)
    runner._parent_call_id = "call_sub"
    return await runner._build_child_ctx(tool_name="subagent_worker")


async def test_subagent_wait_lands_on_parent_clock(tmp_path, monkeypatch):
    mono = _Mono()
    parent = _parent_ctx(tmp_path, monkeypatch)
    clock = TurnClock.start(mono=mono, wall=_wall)
    bind_turn_clock(parent, clock)

    def port(_question):
        mono.now += _THINK
        return "да"

    parent.ask_human = port
    child = await _child_ctx(parent)
    assert current_turn_clock(child) is clock
    assert await _aread_human_response(child, "?") == "да"
    assert clock.elapsed() == 0


async def test_child_without_parent_clock_has_none(tmp_path, monkeypatch):
    parent = _parent_ctx(tmp_path, monkeypatch)
    child = await _child_ctx(parent)
    assert current_turn_clock(child) is None


# --- старт и стоп хода ---------------------------------------------------


def test_main_trace_turn_binds_new_clock():
    ctx, old, _mono = _clocked()
    begin_turn_main_trace(_state(), ctx)
    assert current_turn_clock(ctx) is not None
    assert current_turn_clock(ctx) is not old


def test_child_state_turn_keeps_parent_clock():
    """Субагент открывает свои ходы тем же `begin_turn_main_trace`
    (подхват очереди): часы родителя на `ctx` он не перепривязывает."""
    ctx, clock, _mono = _clocked()
    begin_turn_main_trace(_state(parent_state_id="parent"), ctx)
    assert current_turn_clock(ctx) is clock


def test_stop_run_closes_clock():
    ctx, clock, mono = _clocked()
    executor = AgentExecutor(ctx)
    mono.now += 4
    executor.stop_run()
    mono.now += 10
    assert clock.closed is True
    assert clock.elapsed() == 4
    assert executor.turn_effective_start() == 100.0


def test_executor_without_clock_reads_none():
    executor = AgentExecutor(SimpleNamespace())
    assert executor.turn_effective_start() is None
