"""Футер и выбор модели (спека model-knob §6.5): знак модели, ожидающий
выбор `  ✧ ` и применённый `✦`, который по событию один раз гаснет."""

from types import SimpleNamespace

import pytest
from gc_core.model_choice import ModelChoiceApplied
from gc_core.states import State
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from gc_tui import palette
from gc_tui.app import TuiApp
from gc_tui.palette import ACCENT, BRIGHT, DIM, INK, MUTED, RUN_COLOR
from gc_tui.services.agent_runner import AgentRunner
from gc_tui.services.model_sign import AUTO_COLOR, FREE_COLOR

_REGISTRY = {
    "models": {
        "glm-flash": {},
        "glm-4.6": {},
        "giga-max": {},
        "qwen-coder": {},
    },
    "slots": {"light": "glm-flash", "default": "glm-4.6", "max": "giga-max"},
}
CFG = {
    "agent.model": "m",
    "backend.api_mode": "chat_completions",
    "agent.auto_routing": True,
    **_REGISTRY,
}


def _app(tmp_path, choice, registry=None):
    config = {**CFG, **(registry or {}), "model": choice}
    app = TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(config)
    )
    app._config_path = str(tmp_path / "config.yml")
    return app


def _runner(app, *, level, mode, applied, registry=None):
    """Ход идёт: вызов `level` в режиме `mode`, применён выбор `applied`.
    Живой выбор Router'а рана — выбор окна, санированный реестром рана,
    как у настоящего executor."""
    from gc_core.config_modules.model_registry import (
        configured_choice,
        effective_choice,
    )
    from gc_core.model_router import ModelRouter

    settings = make_settings(registry or _REGISTRY)
    router = ModelRouter(
        settings,
        choice_provider=lambda: level,
        live_choice_provider=lambda: effective_choice(
            configured_choice(app.effective_settings().settings),
            settings.settings,
        ),
    )

    class _Runner:
        def is_running(self):
            return True

        def executor_router(self):
            return router

        def call_level(self):
            return level

        def call_mode(self):
            return mode

        def applied_choice(self):
            return applied

        def usage_snapshot(self):
            return None

    return _Runner()


def _run(app, runner):
    app.tui_state.active_task_id = "t1"
    app.active_runner = runner


def _right(scr) -> str:
    return scr._compose_footer_right().plain


def _style_at(text, substr, start=0):
    idx = text.plain.index(substr, start)
    for span in text.spans:
        if span.start == idx:
            return str(span.style)
    return None


@pytest.mark.asyncio
async def test_manual_footer_shows_model_sign(tmp_path):
    both = {"slots": {**_REGISTRY["slots"], "default": "giga-max"}}
    app = _app(tmp_path, "giga-max", both)
    async with app.run_test(size=(120, 24)):
        head = app.screen._compose_footer_right()
        assert head.plain.startswith("●● giga-max")
        assert _style_at(head, "●") == RUN_COLOR
        assert _style_at(head, "●", 1) == ACCENT
        assert _style_at(head, "giga-max") == INK
    app = _app(tmp_path, "qwen-coder")
    async with app.run_test(size=(120, 24)):
        head = app.screen._compose_footer_right()
        assert head.plain.startswith("● qwen-coder")
        assert _style_at(head, "●") == FREE_COLOR


@pytest.mark.asyncio
async def test_running_auto_footer_keeps_arrow_and_decision_dot(tmp_path):
    both = {"slots": {**_REGISTRY["slots"], "default": "giga-max"}}
    app = _app(tmp_path, "auto", both)
    _run(
        app,
        _runner(
            app,
            level="max",
            mode="auto",
            applied="auto",
            registry={
                **_REGISTRY,
                **both,
            },
        ),
    )
    async with app.run_test(size=(120, 24)):
        head = app.screen._compose_footer_right()
        assert head.plain.startswith("● → ● giga-max")
        assert "✧" not in head.plain
        assert _style_at(head, "●") == AUTO_COLOR
        assert _style_at(head, "●", 1) == ACCENT


@pytest.mark.asyncio
async def test_pending_choice_shows_hollow_star_and_dim_name(tmp_path):
    app = _app(tmp_path, "default")
    _run(app, _runner(app, level="default", mode="manual", applied="default"))
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        assert _right(scr).startswith("● glm-4.6")
        assert "✧" not in _right(scr)
        app.apply_model_choice("max")
        head = scr._compose_footer_right()
        assert head.plain.startswith("● glm-4.6  ✧ ● giga-max")
        assert "→" not in head.plain
        assert _style_at(head, "  ✧ ") == MUTED
        assert _style_at(head, "giga-max") == DIM
        app.apply_model_choice("auto")
        assert _right(scr).startswith("● glm-4.6  ✧ ● auto")
        app.active_runner = None

    app = _app(tmp_path, "auto")
    _run(app, _runner(app, level="max", mode="auto", applied="auto"))
    async with app.run_test(size=(120, 24)):
        app.apply_model_choice("light")
        plain = _right(app.screen)
        assert plain.startswith("● → ● giga-max  ✧ ● glm-flash")
        app.active_runner = None


@pytest.mark.asyncio
async def test_unknown_config_choice_is_not_pending(tmp_path):
    """`model:` вне реестра: ран идёт моделью default (§3.2) — футер
    сравнивает санированный выбор рана, ожидания нет."""
    app = _app(tmp_path, "ghost")
    _run(app, _runner(app, level="default", mode="manual", applied="default"))
    async with app.run_test(size=(120, 24)):
        plain = _right(app.screen)
        assert plain.startswith("● glm-4.6") and "✧" not in plain
        app.active_runner = None


@pytest.mark.asyncio
async def test_no_pending_when_idle(tmp_path):
    app = _app(tmp_path, "default")
    async with app.run_test(size=(120, 24)):
        app.apply_model_choice("max")
        plain = _right(app.screen)
        assert plain.startswith("● giga-max") and "✧" not in plain


def test_applied_shades_single_monotonic_fade():
    shades = [c.lower() for c in palette.MODEL_APPLIED_SHADES]
    assert len(shades) == 8
    assert shades[0] == BRIGHT.lower()
    assert shades[-1] == palette.BG.lower()
    lum = [sum(int(c[i : i + 2], 16) for i in (1, 3, 5)) for c in shades]
    assert all(x > y for x, y in zip(lum, lum[1:]))
    try:
        palette.apply_background((0x21, 0x27, 0x33))
        rebased = palette.MODEL_APPLIED_SHADES
        assert rebased[0].lower() == BRIGHT.lower()
        assert rebased[-1].lower() == "#212733"
    finally:
        palette.reset()


@pytest.mark.asyncio
async def test_applied_event_fades_once_then_clears(tmp_path):
    frames = palette.MODEL_APPLIED_SHADES
    app = _app(tmp_path, "max")
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        scr.on_model_choice_applied(ModelChoiceApplied("max", "giga-max"))
        head = scr._compose_footer_right()
        assert head.plain.startswith("✦ ● giga-max")
        assert _style_at(head, "✦ ") == BRIGHT.lower()
        assert "✦" in str(scr.query_one("#footer-right").render())
        seen = [_style_at(head, "✦ ")]
        for _ in range(len(frames) - 1):
            scr._applied_tick()
            seen.append(_style_at(scr._compose_footer_right(), "✦ "))
        assert seen == [c.lower() for c in frames]
        assert seen[-1] == palette.BG.lower()
        scr._applied_tick()
        assert scr._applied_frame is None
        assert scr._applied_timer is None
        assert _right(scr).startswith("● giga-max")
        scr._refresh_footer()
        assert "✦" not in str(scr.query_one("#footer-right").render())


@pytest.mark.asyncio
async def test_fade_stops_on_session_switch(tmp_path):
    app = _app(tmp_path, "max")
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        scr.on_model_choice_applied(ModelChoiceApplied("max", "giga-max"))
        assert scr._applied_timer is not None
        scr._switch_to_empty_state()
        assert scr._applied_frame is None
        assert scr._applied_timer is None
        assert "✦" not in _right(scr)


@pytest.mark.asyncio
async def test_narrow_footer_keeps_dots_and_stars(tmp_path, monkeypatch):
    app = _app(tmp_path, "auto")
    _run(app, _runner(app, level="max", mode="auto", applied="auto"))
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        app.apply_model_choice("light")
        monkeypatch.setattr(scr, "_context_meter", lambda: None)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 12)
        assert _right(scr) == "● → ●  ✧ ●"
        scr.on_model_choice_applied(ModelChoiceApplied("light", "glm-flash"))
        assert _right(scr) == "✦ ● → ●  ✧ ●"
        app.active_runner = None


def test_already_working_hint_text():
    from gc_tui.services.footer_slot import ALREADY_WORKING_HINT, TRUNCATABLE

    assert ALREADY_WORKING_HINT.format(name="giga-max") == (
        "giga-max уже работает — смены нет"
    )
    assert ALREADY_WORKING_HINT in TRUNCATABLE


def test_runner_applied_choice_reads_state_without_building_executor():
    runner = AgentRunner(
        executor_factory=lambda: pytest.fail("UI must not build executor")
    )
    assert runner.applied_choice() is None
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
    state.applied_choice = "giga-max"
    runner._executor = SimpleNamespace(get_state=lambda: state)
    assert runner.applied_choice() == "giga-max"
    assert state.applied_choice == "giga-max"
