"""Shift+Tab переключает режим разрешений; выбор доезжает до движка."""

import pytest
from gc_testkit.tui_support import tui_settings

pytestmark = pytest.mark.asyncio


def _app(tmp_path, **config):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **config}),
    )


def _footer(pilot) -> str:
    return str(pilot.app.query_one("#footer-left").render())


async def test_shift_tab_cycles_modes_and_the_pill_follows(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        assert pilot.app.effective_permission_mode() == "edit"
        assert pilot.app.permission_mode_source() == "default"
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "manual"
        assert pilot.app.permission_mode_source() == "session"
        assert _footer(pilot).startswith("⏸ manual mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "plan"
        assert _footer(pilot).startswith("☰ plan mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "auto"
        assert _footer(pilot).startswith("⚡ auto mode")
        await pilot.press("shift+tab")
        await pilot.pause()
        assert pilot.app.effective_permission_mode() == "edit"
        assert _footer(pilot).startswith("⏵⏵ edit mode")


async def test_mode_hint_sits_right_after_the_pill(tmp_path):
    """Смена режима на пару секунд дописывает пояснение — одной фразой с пилюлей."""
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("shift+tab")
        await pilot.pause()
        assert (
            _footer(pilot)
            == "⏸ manual mode — подтверждение всех действий, кроме чтения"
        )


async def test_hotkey_disabled_outside_main_screen(tmp_path):
    """На /settings пилюли нет — клавиша не должна молча менять режим."""
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("f6")
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "edit"


async def test_hotkey_disabled_while_confirm_banner_visible(tmp_path):
    """Смысл вопроса подтверждения зависит от режима: посреди него режим не меняют."""
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.screen.show_confirm_banner("t", "q", [])
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "edit"


async def test_hotkey_stays_enabled_during_ask_human(tmp_path):
    """Вопрос агента режима не касается: человек ужесточает режим ПЕРЕД ответом."""
    from gc_tui.state import AskHumanState

    async with _app(tmp_path).run_test() as pilot:
        pilot.app.tui_state.ask_human_state = AskHumanState(
            call_id="c1", question="Q?"
        )
        await pilot.pause()
        await pilot.press("shift+tab")
        assert pilot.app.effective_permission_mode() == "manual"


async def test_config_default_mode_used_when_no_session_choice(tmp_path):
    app = _app(tmp_path, **{"security.permissions.default_mode": "manual"})
    async with app.run_test() as pilot:
        assert pilot.app.effective_permission_mode() == "manual"
        assert pilot.app.permission_mode_source() == "config"
        assert _footer(pilot).startswith("⏸ manual mode")


async def test_thread_meta_mode_adopted(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.adopt_thread_permission_mode(
            {"shared": {"permission_mode": "manual"}}
        )
        assert pilot.app.effective_permission_mode() == "manual"


async def test_unknown_mode_in_thread_meta_is_ignored(tmp_path):
    """Мета из другой сборки или режим, которого больше нет: движок такой
    режим отвергает исключением, и открытие сессии не должно на нём падать."""
    async with _app(tmp_path).run_test() as pilot:
        sent = []
        pilot.app.set_permission_mode = sent.append
        pilot.app.adopt_thread_permission_mode(
            {"shared": {"permission_mode": "bypass"}}
        )
        assert sent == []
        pilot.app.adopt_thread_permission_mode(
            {"shared": {"permission_mode": " Manual "}}
        )
        assert sent == ["manual"]


async def test_startup_flag_wins_over_thread_meta(tmp_path):
    app = _app(tmp_path)
    app.lock_permission_mode("manual")  # эквивалент --permission-mode
    async with app.run_test() as pilot:
        pilot.app.adopt_thread_permission_mode(
            {"shared": {"permission_mode": "edit"}}
        )
        assert pilot.app.effective_permission_mode() == "manual"


class _FakeExecutor:
    """Движок: помнит режим так же, как AgentExecutor.set_permission_mode."""

    def __init__(self) -> None:
        self.mode = None

    def set_permission_mode(self, mode: str) -> None:
        self.mode = mode


def _engine_mode(app) -> str | None:
    runner = app.get_runner()
    runner._ensure_executor()
    return runner._executor.mode


async def test_mode_reaches_engine_of_freshly_built_runner(tmp_path):
    """Раннера в момент хоткея может не быть (после /stop, перечитки конфига):
    свежий раннер обязан получить текущий выбор при создании."""
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        await pilot.press("shift+tab")
        pilot.app.invalidate_runner()
        assert _engine_mode(pilot.app) == "manual"


async def test_mode_reaches_engine_of_live_runner(tmp_path):
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        assert (
            _engine_mode(pilot.app) is None
        )  # выбора не было — движок на конфиге
        await pilot.press("shift+tab")
        assert _engine_mode(pilot.app) == "manual"


async def test_startup_flag_and_thread_meta_reach_engine(tmp_path):
    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    app.lock_permission_mode("manual")
    async with app.run_test() as pilot:
        assert _engine_mode(pilot.app) == "manual"

    app = _app(tmp_path)
    app._executor_factory = _FakeExecutor
    async with app.run_test() as pilot:
        pilot.app.adopt_thread_permission_mode(
            {"shared": {"permission_mode": "manual"}}
        )
        assert _engine_mode(pilot.app) == "manual"
