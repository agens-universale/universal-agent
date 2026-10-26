"""Футер и выбор модели (спека model-knob §6.5, раунд 3 смока): строка 1
— знак модели хода; выбор, ждущий ближайшего шага, — во второй строке
справа знаком с тусклым именем: имя в колонке имени строки 1, точки
прижаты к нему. Применение — сразу, без анимации."""

from types import SimpleNamespace

import pytest
from gc_core.states import State
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings
from gc_tui import palette
from gc_tui.app import TuiApp
from gc_tui.palette import ACCENT, DIM, INK, RUN_COLOR
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


def _rows(scr) -> list[str]:
    return _right(scr).split("\n")


def _assert_aligned(row1: str, row2: str, name1: str, name2: str) -> None:
    """Имена в одной колонке, точки выбора прижаты к имени, строки одной
    ширины (колонка выровнена вправо)."""
    col = row2.index(name2)
    assert row1.index(name1) == col
    assert row2[col - 2 : col] == "● "
    assert row2[: col - 2].rstrip("●").strip() == ""
    assert len(row1) == len(row2)


@pytest.mark.asyncio
async def test_pending_choice_goes_to_second_row_under_name(tmp_path):
    app = _app(tmp_path, "default")
    _run(app, _runner(app, level="default", mode="manual", applied="default"))
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        assert _rows(scr) == [_rows(scr)[0]]
        app.apply_model_choice("max")
        head = scr._compose_footer_right()
        row1, row2 = head.plain.split("\n")
        assert row1.startswith("● glm-4.6")
        assert "✧" not in head.plain and "→" not in head.plain
        _assert_aligned(row1, row2, "glm-4.6", "giga-max")
        second = head.plain.index("giga-max")
        assert _style_at(head, "giga-max", second) == DIM
        assert _style_at(head, "●", second - 2) == ACCENT
        # manual → auto: слово `auto` в колонке имени.
        app.apply_model_choice("auto")
        row1, row2 = _rows(scr)
        _assert_aligned(row1, row2, "glm-4.6", "auto")
        app.active_runner = None


@pytest.mark.asyncio
async def test_pending_wider_dots_shift_first_row(tmp_path):
    """Точек выбора больше, чем у модели хода: строка 1 сдвигается, имена
    остаются в одной колонке."""
    both = {"slots": {**_REGISTRY["slots"], "default": "giga-max"}}
    registry = {**_REGISTRY, **both}
    app = _app(tmp_path, "light", both)
    _run(
        app,
        _runner(
            app,
            level="light",
            mode="manual",
            applied="light",
            registry=registry,
        ),
    )
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        app.apply_model_choice("max")
        row1, row2 = _rows(scr)
        assert row1.startswith(" ● glm-flash")
        assert row2.startswith("●● giga-max")
        _assert_aligned(row1, row2, "glm-flash", "giga-max")
        app.active_runner = None


@pytest.mark.asyncio
async def test_pending_under_running_auto_aligns_with_decision(tmp_path):
    app = _app(tmp_path, "auto")
    _run(app, _runner(app, level="max", mode="auto", applied="auto"))
    async with app.run_test(size=(120, 24)):
        app.apply_model_choice("light")
        row1, row2 = _rows(app.screen)
        assert row1.startswith("● → ● giga-max")
        _assert_aligned(row1, row2, "giga-max", "glm-flash")
        app.active_runner = None


def _prices(scr, monkeypatch) -> None:
    from rich.text import Text

    monkeypatch.setattr(scr, "_context_meter", lambda: None)
    monkeypatch.setattr(
        scr,
        "_usage_segments",
        lambda: (Text("запрос 1k/0.1₽"), Text("сессия 2k/0.2₽")),
    )


@pytest.mark.asyncio
async def test_pending_keeps_prices_on_first_row_when_wide(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, "default")
    _run(app, _runner(app, level="default", mode="manual", applied="default"))
    async with app.run_test(size=(160, 24)):
        scr = app.screen
        _prices(scr, monkeypatch)
        app.apply_model_choice("max")
        row1, row2 = _rows(scr)
        assert "запрос" in row1 and "сессия" in row1
        assert "запрос" not in row2
        _assert_aligned(row1, row2, "glm-4.6", "giga-max")
        app.active_runner = None


@pytest.mark.asyncio
async def test_pending_takes_prices_place_on_second_row_when_narrow(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, "default")
    runner = _runner(app, level="default", mode="manual", applied="default")
    _run(app, runner)
    async with app.run_test(size=(160, 24)):
        scr = app.screen
        _prices(scr, monkeypatch)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 34)
        assert _rows(scr) == ["● glm-4.6", "запрос 1k/0.1₽ · сессия 2k/0.2₽"]
        app.apply_model_choice("max")
        row1, row2 = _rows(scr)
        assert row1.rstrip() == "● glm-4.6"
        assert "запрос" not in _right(scr) and "сессия" not in _right(scr)
        _assert_aligned(row1, row2, "glm-4.6", "giga-max")
        app.active_runner = None


@pytest.mark.asyncio
async def test_unknown_config_choice_is_not_pending(tmp_path):
    """`model:` вне реестра: ран идёт моделью default (§3.2) — футер
    сравнивает санированный выбор рана, ожидания нет."""
    app = _app(tmp_path, "ghost")
    _run(app, _runner(app, level="default", mode="manual", applied="default"))
    async with app.run_test(size=(120, 24)):
        plain = _right(app.screen)
        assert plain.startswith("● glm-4.6") and "\n" not in plain
        app.active_runner = None


@pytest.mark.asyncio
async def test_no_pending_when_idle(tmp_path):
    app = _app(tmp_path, "default")
    async with app.run_test(size=(120, 24)):
        app.apply_model_choice("max")
        plain = _right(app.screen)
        assert plain.startswith("● giga-max") and "\n" not in plain


@pytest.mark.asyncio
async def test_narrow_footer_squeezes_pending_name_like_current(
    tmp_path, monkeypatch
):
    """Узко: имя выбора во второй строке ужимается по середине тем же
    потолком, что имя модели хода, — колонки имён совпадают."""
    current, chosen = (
        "provider/long-model-alpha-1",
        "provider/long-model-beta-222",
    )
    registry = {
        "models": {current: {}, chosen: {}},
        "slots": {"default": current, "max": chosen},
    }
    app = _app(tmp_path, "default", registry)
    _run(
        app,
        _runner(
            app,
            level="default",
            mode="manual",
            applied="default",
            registry=registry,
        ),
    )
    async with app.run_test(size=(160, 24)):
        scr = app.screen
        monkeypatch.setattr(scr, "_context_meter", lambda: None)
        avail = len(current) + 2
        monkeypatch.setattr(
            scr, "_footer_right_available_width", lambda: avail
        )
        app.apply_model_choice("max")
        row1, row2 = _rows(scr)
        assert "…" in row2 and row2.lstrip().startswith("● provider/")
        assert len(row2) <= avail
        col = row2.index("provider/")
        assert row1.index("provider/") == col
        app.active_runner = None


def test_no_applied_fade_ramp_in_palette():
    assert not hasattr(palette, "MODEL_APPLIED_SHADES")
    assert "MODEL_APPLIED_SHADES" not in palette._RAMP_PEAKS


@pytest.mark.asyncio
async def test_applied_choice_switches_footer_at_once(tmp_path):
    """Применение: строка 1 — сразу новая модель, вторая строка уходит;
    ни кадров, ни таймера угасания."""
    from gc_tui.screens import main as main_screen

    assert not hasattr(main_screen, "_APPLIED_STEP_S")
    app = _app(tmp_path, "default")
    runner = _runner(app, level="default", mode="manual", applied="default")
    _run(app, runner)
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        app.apply_model_choice("max")
        assert len(_rows(scr)) == 2
        applied = _runner(app, level="max", mode="manual", applied="max")
        app.active_runner = applied
        rows = _rows(scr)
        assert rows == [rows[0]] and rows[0].startswith("● giga-max")
        assert "✦" not in _right(scr)
        for name in ("_applied_frame", "_applied_timer", "_applied_tick"):
            assert not hasattr(scr, name)
        app.active_runner = None


@pytest.mark.asyncio
async def test_narrow_footer_keeps_dots_aligned(tmp_path, monkeypatch):
    app = _app(tmp_path, "auto")
    _run(app, _runner(app, level="max", mode="auto", applied="auto"))
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        app.apply_model_choice("light")
        monkeypatch.setattr(scr, "_context_meter", lambda: None)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 12)
        assert _right(scr) == "● → ●\n    ●"
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
