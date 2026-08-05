"""confirm banner: показ/скрытие + перехват y/s/N/Esc/↑↓/Enter → submit_confirm_reply."""
import pytest


class _StubRunner:
    """Минимальный runner-stub: фиксирует вызовы submit_confirm_reply."""

    def __init__(self, running: bool = True) -> None:
        self.replies: list[str] = []
        self._running = running
        self.cancel_confirm_calls: int = 0
        self.stopped: bool = False

    def is_running(self) -> bool:
        return self._running

    def submit_confirm_reply(self, choice: str) -> None:
        self.replies.append(choice)

    def cancel_confirm(self) -> None:
        self.cancel_confirm_calls += 1

    def stop(self) -> None:
        self.stopped = True


@pytest.mark.asyncio
async def test_confirm_banner_y_answers_once(tmp_path):
    """«y» при активном баннере: submit_confirm_reply("once"), баннер снят."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        # Баннер смонтирован, input-row скрыт.
        assert scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 1
        assert pilot.app.query_one("#input-row", Horizontal).has_class("hidden")

        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == ["once"]
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row восстановлен
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("hidden")


@pytest.mark.asyncio
async def test_confirm_banner_s_answers_session(tmp_path):
    """«s» при активном баннере: submit_confirm_reply("session")."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("s")
        await pilot.pause(0.1)

        assert runner.replies == ["session"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_n_answers_deny(tmp_path):
    """«n» при активном баннере: submit_confirm_reply("deny")."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("n")
        await pilot.pause(0.1)

        assert runner.replies == ["deny"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_escape_answers_deny(tmp_path):
    """Esc при активном баннере: submit_confirm_reply("deny"), не проваливается
    в глобальную Esc-лестницу (нет exit-модалки)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("escape")
        await pilot.pause(0.1)

        assert runner.replies == ["deny"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # Не открылась exit-модалка (screen_stack == 1)
        assert len(pilot.app.screen_stack) == 1


@pytest.mark.asyncio
async def test_confirm_banner_enter_default_answers_once(tmp_path):
    """Enter без навигации (selected==0): submit_confirm_reply("once")."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert runner.replies == ["once"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_down_then_enter_answers_session(tmp_path):
    """↓ + Enter (selected==1): submit_confirm_reply("session")."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("down")
        await pilot.pause(0.05)
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert runner.replies == ["session"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_enter_not_open_tool_detail_when_row_focused(tmp_path):
    """Регресс: фокус на running ToolCallRow + confirm → Enter/Y отвечают
    баннеру, а не открывают ToolCallDetailScreen.

    Раньше Key шёл в ClickModelMixin.on_key строки (stop) до Screen.on_key,
    и Enter открывал viewer деталей вызова вместо once/deny.
    """
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.models import TraceEvent
    from tui.screens.tool_call_detail import ToolCallDetailScreen
    from tui.widgets.confirm_banner import ConfirmBanner
    from tui.widgets.tool_call_row import ToolCallRow

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        call_ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="call",
            title="exec_command",
            summary="curl",
            full_payload={
                "type": "function_call",
                "name": "exec_command",
                "arguments": '{"command":"curl"}',
                "call_id": "c1",
            },
            call_id="c1",
            tool_name="exec_command",
        )
        scr._task_events = [call_ev]
        row = ToolCallRow(
            call_id="c1", tool_name="exec_command", args_summary="curl",
        )
        await scr.query_one("#trace").mount(row)
        await pilot.pause(0.05)

        scr.show_confirm_banner("Команда", "Разрешить?", [("Command", "curl")])
        await pilot.pause(0.05)
        # Симулируем клик/Tab по строке тула во время confirm (фокус украден).
        row.focus()
        await pilot.pause(0.05)
        assert isinstance(pilot.app.focused, ToolCallRow)

        await pilot.press("enter")
        await pilot.pause(0.1)

        stack = [type(s).__name__ for s in pilot.app.screen_stack]
        assert "ToolCallDetailScreen" not in stack, stack
        assert not any(isinstance(s, ToolCallDetailScreen) for s in pilot.app.screen_stack)
        assert runner.replies == ["once"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_receives_focus_on_show(tmp_path):
    """show_confirm_banner ставит фокус на баннер (не None / не tool-row)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner
    from tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        row = ToolCallRow(call_id="c1", tool_name="exec_command", args_summary="x")
        await scr.query_one("#trace").mount(row)
        row.focus()
        await pilot.pause(0.05)

        scr.show_confirm_banner("T", "Q?")
        await pilot.pause(0.05)

        assert isinstance(pilot.app.focused, ConfirmBanner)


# ── Tests for Ctrl+C / _reset_render_state confirm handling ──────────────────

@pytest.mark.asyncio
async def test_ctrl_c_during_init_style_confirm_cancels_and_hides_banner(tmp_path):
    """/init-стиль: _confirm_active=True, runner.is_running()==False.
    Ctrl+C → cancel_confirm вызван, баннер снят, run НЕ остановлен (не было живого)."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner(running=False)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Init", "Инициализировать?")
        await pilot.pause(0.1)

        assert scr._confirm_active

        scr._cmd_stop()
        await pilot.pause(0.1)

        # cancel_confirm unblocked the parked thread
        assert runner.cancel_confirm_calls == 1
        # banner removed
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row restored
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("hidden")
        # run was NOT stopped (no live runner)
        assert not runner.stopped


@pytest.mark.asyncio
async def test_ctrl_c_during_live_agent_confirm_cancels_and_stops_run(tmp_path):
    """live-agent: _confirm_active=True, runner.is_running()==True.
    Ctrl+C → cancel_confirm вызван, баннер снят, раннер остановлен."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner(running=True)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Агент", "Разрешить?")
        await pilot.pause(0.1)

        assert scr._confirm_active

        scr._cmd_stop()
        await pilot.pause(0.1)

        # cancel_confirm called
        assert runner.cancel_confirm_calls == 1
        # banner removed
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row restored
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("hidden")
        # active_runner nulled by _stop_and_replace_runner
        assert pilot.app.active_runner is None


@pytest.mark.asyncio
async def test_reset_render_state_clears_confirm_banner(tmp_path):
    """Backstop: _reset_render_state при _confirm_active=True → баннер снят, cancel вызван."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner(running=False)
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Reset", "Сбросить?")
        await pilot.pause(0.1)

        assert scr._confirm_active

        scr._reset_render_state()
        await pilot.pause(0.1)

        assert runner.cancel_confirm_calls == 1
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row restored
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("hidden")
