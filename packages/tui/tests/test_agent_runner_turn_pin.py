"""Футер читает факт подготовленного вызова и выбор следующего через AgentRunner."""

from types import SimpleNamespace

import pytest

from gc_testkit.tui_support import tui_settings

from gc_core.states import State
from gc_core.turn_boundary import begin_turn
from gc_tui.app import TuiApp
from gc_tui.services.agent_runner import AgentRunner


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "start,selected,pending,next_footer",
    [
        ("light", "max", "● fast → ● strong", "● strong"),
        ("max", "light", "● strong → ● fast", "● fast"),
        ("light", "auto", "● fast → ● auto", "● → ● strong"),
        ("max", "auto", "● strong → ● auto", "● → ● strong"),
        ("auto", "light", "● → ● strong → ● fast", "● fast"),
        ("auto", "max", "● → ● strong → ● strong", "● strong"),
        ("light", "default", "● fast → ● strong", "● strong"),
        ("default", "light", "● strong → ● fast", "● fast"),
        ("default", "max", "● strong → ● strong", "● strong"),
        ("max", "default", "● strong → ● strong", "● strong"),
        ("default", "auto", "● strong → ● auto", "● → ● strong"),
        ("auto", "default", "● → ● strong → ● strong", "● strong"),
    ],
)
async def test_footer_keeps_prepared_call_and_applies_selection_to_next_call(
    tmp_path, monkeypatch, start, selected, pending, next_footer
):
    from gc_core.agent_executor import AgentExecutor
    from gc_core.auto_routing.state_ops import close_tick
    from gc_core.processing_responses import aprepare_main_call
    from gc_core.run_context import acreate_run_context

    loaded = tui_settings(
        {
            "agent.model": "m",
            "level": start,
            "backend.api_mode": "chat_completions",
            "logging.save_logs": False,
            "models": {"fast": {}, "strong": {}},
            "slots": {"light": "fast", "default": "strong", "max": "strong"},
        }
    )
    app = TuiApp(settings=loaded, threads_dir=str(tmp_path / "threads"))
    app._config_path = str(tmp_path / "config.yml")
    ctx = await acreate_run_context(
        loaded, is_tui=True, choice_provider=app._choice_provider
    )
    state = State(
        verbose=False,
        model="m",
        tools_spec=ctx.tools_spec,
        tools_descr=ctx.tools_description,
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    executor = AgentExecutor(ctx)
    executor._bind_state(state, discard_writes_after_stop=False)
    begin_turn(state, ctx)
    first = await aprepare_main_call(state, ctx)
    first_level = state._call_level
    runner = AgentRunner(executor=executor)
    monkeypatch.setattr(runner, "is_running", lambda: True)
    app.active_runner = runner
    app.tui_state.active_task_id = "t1"
    monkeypatch.setattr(app, "collect_footer_notices", list)
    async with app.run_test(size=(120, 24)):
        app.apply_session_override("level", selected)
        assert app.screen._compose_footer_right().plain == pending
        assert state._call_level == first_level
        assert (await aprepare_main_call(state, ctx)).alias == first.alias
        assert app.screen._compose_footer_right().plain == pending

        close_tick(state, first_level)
        await aprepare_main_call(state, ctx)
        assert state.turn_level is None
        assert app.screen._compose_footer_right().plain == next_footer

        app.apply_session_override("level", start)
        close_tick(state, state._call_level)
        await aprepare_main_call(state, ctx)
        expected = (
            "● → ● strong"
            if start == "auto"
            else "● fast"
            if start == "light"
            else "● strong"
        )
        assert app.screen._compose_footer_right().plain == expected
        app.active_runner = None


def test_reading_call_does_not_build_executor_or_mutate_state():
    runner = AgentRunner(
        executor_factory=lambda: pytest.fail("UI must not build executor")
    )
    assert runner.call_level() is None
    assert runner.executor_router() is None


def test_runner_reads_prepared_call_level_and_mode_without_building_executor():
    runner = AgentRunner(
        executor_factory=lambda: pytest.fail("UI must not build executor")
    )
    assert runner.call_level() is None
    assert runner.call_mode() is None
    state = State(
        verbose=False,
        model="m",
        tools_spec=[],
        tools_descr=[],
        skills_list=[],
        max_steps=10,
        system_prompt_id="test",
        use_server_subagents=False,
    )
    state.turn_level = (
        "max"  # legacy compatibility field must not drive the UI
    )
    state._call_level = "light"
    state._tick_decision = ("auto", True, None, "light")
    runner._executor = SimpleNamespace(get_state=lambda: state)
    assert runner.call_level() == "light"
    assert runner.call_mode() == "auto"
    state._tick_decision = ("max", False, None, "max")
    state._call_level = "max"
    assert runner.call_level() == "max"
    assert runner.call_mode() == "manual"
    state._tick_decision = None
    assert runner.call_mode() is None
    assert (
        state.turn_level == "max"
    )  # presentation does not mutate compatibility state
