"""Футер режима `auto` (спека auto-routing §7.1): точка режима + факт бегущего
хода (`● → ● модель`), лесенка ширины (текст → стрелка/точка уровня → голая
точка режима). Обвязка `TuiApp` — инлайн, как в `test_footer_profile_indicator.py`;
тестовые файлы друг друга не импортируют."""

import pytest

from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings

from gc_tui.app import TuiApp
from gc_tui.palette import DIM, INK
from gc_tui.screens.main import _FooterHeadSnapshot
from gc_tui.services.model_sign import AUTO_COLOR


def _app(tmp_path, config):
    app = TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(config)
    )
    app._config_path = str(tmp_path / "config.yml")
    return app


def _set_running(app, runner):
    """Ход считается идущим по `_run_in_progress()` (footer-left «Ctrl+C/stop»,
    спека §7.1) — предикату нужны ОБА условия: активная задача И живой раннер,
    не только `runner.is_running()`. Тесты бегущего хода ставят их парой."""
    app.tui_state.active_task_id = "t1"
    app.active_runner = runner


def _running_runner(
    router, *, current_level=None, usage_snapshot=None, available=True
):
    """Раннер бегущего хода (образец `_running_runner` из
    `test_footer_profile_indicator.py`, расширенный auto-routing §7.1
    изменяемым `current_level`): `executor_router()` резолвит ЧЕСТНО через
    переданный `router` (breaker/slots настоящего Router'а приложения),
    подменяя только `active_target()` — читает `.level` СВЕЖИМ на каждый
    вызов, как `state.current_level` у настоящего исполнителя (`level = None`
    → «до первого решения», `effective_target` подставит `default`).
    `available` — доступность auto для симулируемого решения (по умолчанию
    True; тест недоступной фичи передаёт False явно, а не полагается на
    реальный конфиг — который сюда не долетает, `router` уже построен)."""
    from gc_core.auto_routing.levels import effective_target

    class _FakeRunner:
        def __init__(self):
            self.level = current_level

        def is_running(self):
            return True

        def executor_router(self):
            runner = self

            class _LevelRouter:
                def active_target(self):
                    return effective_target(
                        router.live_choice(), runner.level, available
                    )

                def __getattr__(self, name):
                    return getattr(router, name)

            return _LevelRouter()

        def usage_snapshot(self):
            return usage_snapshot

    return _FakeRunner()


CFG = {
    "agent.model": "m",
    "model": "auto",
    "backend.api_mode": "chat_completions",
    "agent.auto_routing": True,
    "models": {"fast": {}, "mid": {}, "big": {}},
    "slots": {"light": "fast", "default": "mid", "max": "big"},
}


@pytest.mark.asyncio
async def test_footer_idle_auto_shows_mode_only(tmp_path, monkeypatch):
    app = _app(tmp_path, CFG)
    app.active_runner = None
    async with app.run_test(size=(120, 24)):
        plain = app.screen._compose_footer_right().plain
        assert "● auto" in plain and "→" not in plain and "mid" not in plain


@pytest.mark.asyncio
async def test_footer_running_auto_shows_arrow_level_dot_and_model(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, CFG)
    runner = _running_runner(app.display_router(), current_level="light")
    _set_running(app, runner)
    async with app.run_test(size=(120, 24)):
        plain = app.screen._compose_footer_right().plain
        assert "● → ● fast" in plain and "auto" not in plain
        runner.level = "max"
        assert "● → ● big" in app.screen._compose_footer_right().plain


@pytest.mark.asyncio
async def test_footer_running_auto_before_first_decision_shows_default(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, CFG)
    _set_running(
        app, _running_runner(app.display_router(), current_level=None)
    )
    async with app.run_test(size=(120, 24)):
        assert "● → ● mid" in app.screen._compose_footer_right().plain


@pytest.mark.asyncio
async def test_footer_running_auto_before_executor_built_shows_default(
    tmp_path, monkeypatch
):
    """Спека §7.1: «идёт ход» — это `runner.is_running()`, а не «резолв Router'а
    executor'а удался». Пока executor строится в worker-потоке
    (`AgentRunner._ensure_executor()` зовётся из `start()`, уже после того как
    `is_running()` стал True), `executor_router()` честно отдаёт None — футер
    обязан остаться в виде факта (стрелка + точка default-слота из живого
    резолва витрины), а не откатиться к виду режима (`● auto`)."""

    class _BuildingRunner:
        def is_running(self):
            return True

        def executor_router(self):
            return None

        def usage_snapshot(self):
            return None

    app = _app(tmp_path, CFG)
    _set_running(app, _BuildingRunner())
    async with app.run_test(size=(120, 24)):
        assert "● → ● mid" in app.screen._compose_footer_right().plain


@pytest.mark.asyncio
async def test_footer_running_auto_degraded_resolve_shows_default(
    tmp_path, monkeypatch
):
    """Та же деградация факта (спека §7.1), другой источник: резолв Router'а
    executor'а бросает `ValueError` (реестр не резолвится — больше одной
    модели, ни один слот не назначен) — `_footer_running_resolution()` отдаёт
    None, футер остаётся в виде факта через живой резолв витрины, а не режима."""
    from gc_core.model_router import ModelRouter

    broken = ModelRouter(
        make_settings({"models": {"x": {}, "y": {}}, "slots": {}}),
        choice_provider=lambda: "light",
    )

    class _DegradedRunner:
        def is_running(self):
            return True

        def executor_router(self):
            return broken

        def usage_snapshot(self):
            return None

    app = _app(tmp_path, CFG)
    _set_running(app, _DegradedRunner())
    async with app.run_test(size=(120, 24)):
        assert "● → ● mid" in app.screen._compose_footer_right().plain


@pytest.mark.asyncio
async def test_footer_auto_unavailable_looks_like_manual_default(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, {**CFG, "agent.auto_routing": False})
    app.active_runner = None
    async with app.run_test(size=(120, 24)):
        plain = app.screen._compose_footer_right().plain
        assert "● mid" in plain and "auto" not in plain and "→" not in plain


@pytest.mark.asyncio
async def test_footer_auto_unavailable_running_looks_like_manual_default(
    tmp_path, monkeypatch
):
    """Та же строка таблицы §7.1 («level: auto, фича недоступна» = как ручной
    default), но во время хода: тоже без стрелки, просто модель — auto-режим
    не включает блок перехода, если фича выключена, идёт ход или нет."""
    app = _app(tmp_path, {**CFG, "agent.auto_routing": False})
    runner = _running_runner(
        app.display_router(), current_level="light", available=False
    )
    _set_running(app, runner)
    async with app.run_test(size=(120, 24)):
        plain = app.screen._compose_footer_right().plain
        assert "● mid" in plain and "auto" not in plain and "→" not in plain


@pytest.mark.asyncio
async def test_footer_auto_ladder_drops_model_then_level_block_keeps_mode_dot(
    tmp_path, monkeypatch
):
    app = _app(tmp_path, CFG)
    _set_running(
        app, _running_runner(app.display_router(), current_level="light")
    )
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        # "● → ● fast" = 10 колонок, "● → ●" = 5 колонок — ширина 7 попадает
        # между ними и снимает только имя модели.
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 7)
        step1 = scr._compose_footer_right().plain
        assert "● → ●" in step1 and "fast" not in step1
        # "●" = 1 колонка — ширина 3 ниже диапазона "● → ●" (5-9), снимает и
        # стрелку с точкой уровня.
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 3)
        step2 = scr._compose_footer_right().plain
        assert "●" in step2 and "→" not in step2


@pytest.mark.asyncio
async def test_footer_manual_unchanged(tmp_path, monkeypatch):
    app = _app(tmp_path, {**CFG, "model": "max"})
    app.active_runner = None
    async with app.run_test(size=(120, 24)):
        plain = app.screen._compose_footer_right().plain
        assert "● big" in plain and "→" not in plain and "auto" not in plain


def _span_style(text, substr):
    """Стиль спана, начинающегося ровно на первом вхождении `substr` — тот же
    приём построения, что и у `_footer_head` (append per-run)."""
    idx = text.plain.index(substr)
    for span in text.spans:
        if span.start == idx:
            return span.style
    return None


@pytest.mark.asyncio
async def test_footer_head_auto_colors_mode_dot_arrow_and_word(tmp_path):
    """Цвета (спека §7.1): точка режима — `bold {AUTO_COLOR}`
    (единственный источник цвета `auto`, не литерал BRAND); хода нет — слово
    `auto` INK; идёт ход — стрелка DIM, точка режима остаётся BRAND (точка
    уровня — отдельным цветом снимка, здесь не проверяется)."""
    app = _app(tmp_path, CFG)
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        idle = scr._footer_head(
            with_profile=False,
            meter=None,
            snapshot=_FooterHeadSnapshot(None, True, "#123456", "mid", False),
        )
        assert _span_style(idle, "●") == f"bold {AUTO_COLOR}"
        assert _span_style(idle, " auto") == INK

        running = scr._footer_head(
            with_profile=False,
            meter=None,
            snapshot=_FooterHeadSnapshot(None, True, "#123456", "fast", True),
        )
        assert _span_style(running, "●") == f"bold {AUTO_COLOR}"
        assert _span_style(running, " → ") == DIM


@pytest.mark.asyncio
async def test_pending_transition_keeps_the_narrowest_rung_without_level_block(
    tmp_path, monkeypatch
):
    """Пока ждёт переход, имя модели не снимается — но самая узкая ступень
    auto (без блока уровня «→ ●») остаётся: иначе на совсем узком окне правая
    колонка не влезала и поджимала левую (глиф режима, стоп-клавишу)."""
    app = _app(tmp_path, CFG)
    _set_running(
        app, _running_runner(app.display_router(), current_level="light")
    )
    async with app.run_test(size=(120, 24)):
        scr = app.screen
        snapshot = _FooterHeadSnapshot(
            None,
            True,
            "#123456",
            "gatewayhub/acme-lab/acme-big-coder-4.5",
            True,
            ("#654321", "big"),
        )
        monkeypatch.setattr(scr, "_footer_head_snapshot", lambda: snapshot)
        monkeypatch.setattr(scr, "_footer_right_available_width", lambda: 3)

        plain = scr._compose_footer_right().plain

        assert "→ ●" not in plain
