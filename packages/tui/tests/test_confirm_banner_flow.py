"""confirm banner: показ/скрытие + перехват y/s/N/Esc/↑↓/Enter → submit_confirm_reply."""

from gc_testkit.tui_support import tui_settings
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


def _tool_call_events():
    """Пара call+result для call_id="c1" — из неё Screen строит ToolCallDetailScreen."""
    from gc_tui.models import TraceEvent

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
    result_ev = TraceEvent(
        no=2,
        event_ts_utc_iso="t",
        kind="result",
        title="exec_command",
        summary="ok",
        full_payload={
            "type": "function_call_output",
            "call_id": "c1",
            "output": "ok",
        },
        call_id="c1",
        tool_name="exec_command",
    )
    return [call_ev, result_ev]


async def _settle(pilot) -> None:
    """Дождаться простоя приложения (layout, call_after_refresh, очереди сообщений).

    pilot.pause(N) — это просто sleep; фиксированные миллисекунды под нагрузкой
    делают негативную проверку «экран не открылся» ложно-зелёной («ещё не успел»)
    и двигают строку под курсором уже после расчёта точки клика.
    """
    for _ in range(3):
        await pilot.pause()
        await pilot.pause(0.05)


async def _mouse_click(pilot, row, *, ctrl: bool = False) -> None:
    """Настоящий клик мышью по строке трейса: MouseDown/MouseUp/Click идут через
    screen._forward_event (pilot.click), а НЕ row.focus().

    Клик по фокусу — не клик: именно подмена жеста фокусом спрятала дыру в
    мышиной половине confirm-гейта. Строку сначала подводим в зону видимости
    (в трейсе сверху приветственный #brand-block) и проверяем, что курсор
    действительно попал в неё — иначе тест «прошёл бы» на промахе.
    """
    row.scroll_visible(animate=False)
    await _settle(pilot)
    hit, _ = pilot.app.get_widget_at(*row.region.offset)
    assert row in hit.ancestors_with_self, hit
    await pilot.click(row, control=ctrl)
    await _settle(pilot)


@pytest.mark.asyncio
async def test_confirm_banner_y_answers_allow_remember(tmp_path):
    """«y» при активном баннере: submit_confirm_reply("allow_remember"), баннер снят."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from textual.containers import Horizontal

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        # Баннер смонтирован, input-row скрыт.
        assert scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 1
        assert pilot.app.query_one("#input-row", Horizontal).has_class(
            "hidden"
        )

        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row восстановлен
        assert not pilot.app.query_one("#input-row", Horizontal).has_class(
            "hidden"
        )


@pytest.mark.asyncio
async def test_confirm_banner_o_answers_allow_once(tmp_path):
    """«o» при активном баннере: submit_confirm_reply("allow_once")."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("o")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_once"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_n_answers_deny_remember(tmp_path):
    """«n» при активном баннере: submit_confirm_reply("deny_remember")."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("n")
        await pilot.pause(0.1)

        assert runner.replies == ["deny_remember"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_escape_answers_deny_once(tmp_path):
    """Esc при активном баннере — отмена: submit_confirm_reply("deny_once"), не
    запрет на всю сессию (его выбирают явно, клавишей N), и не проваливается в
    глобальную Esc-лестницу (нет exit-модалки)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("escape")
        await pilot.pause(0.1)

        assert runner.replies == ["deny_once"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # Не открылась exit-модалка (screen_stack == 1)
        assert len(pilot.app.screen_stack) == 1


@pytest.mark.asyncio
async def test_confirm_banner_enter_default_answers_allow_remember(tmp_path):
    """Enter без навигации (selected==0): submit_confirm_reply("allow_remember")."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("enter")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_down_then_enter_answers_allow_once(tmp_path):
    """↓ + Enter (selected==1): submit_confirm_reply("allow_once")."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("down")
        await pilot.pause(0.05)
        await pilot.press("enter")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_once"]
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
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from gc_tui.widgets.tool_call_row import ToolCallRow

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
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
            call_id="c1",
            tool_name="exec_command",
            args_summary="curl",
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
        assert not any(
            isinstance(s, ToolCallDetailScreen) for s in pilot.app.screen_stack
        )
        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_ctrl_click_not_open_tool_detail(tmp_path):
    """Регресс (мышиная половина): Ctrl+КЛИК по ToolCallRow при активном confirm
    НЕ пушит ToolCallDetailScreen поверх баннера.

    Клавиатурный гейт (Enter) уже стоял, мышиный — нет: экран события накрывал
    баннер, «y» уходил в накрывший экран, а агент продолжал ждать ответа.
    Здесь диспатчится НАСТОЯЩИЙ events.Click (не row.focus()) — именно этот
    жест дырку и открывал.
    """
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from gc_tui.widgets.tool_call_row import ToolCallRow

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._task_events = _tool_call_events()
        row = ToolCallRow(
            call_id="c1", tool_name="exec_command", args_summary="curl"
        )
        await scr.query_one("#trace").mount(row)
        row.set_result(summary="ok", error=False)
        await pilot.pause(0.05)

        scr.show_confirm_banner("Команда", "Разрешить?", [("Command", "curl")])
        await pilot.pause(0.05)
        stack_before = len(pilot.app.screen_stack)

        await _mouse_click(pilot, row, ctrl=True)

        assert len(pilot.app.screen_stack) == stack_before
        assert not any(
            isinstance(s, ToolCallDetailScreen) for s in pilot.app.screen_stack
        )
        assert scr._confirm_active

        # Баннер по-прежнему отвечает на «y».
        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_ctrl_click_opens_tool_detail_without_confirm(tmp_path):
    """Контроль: без confirm Ctrl+клик по ToolCallRow по-прежнему открывает
    детальный экран — гейт не выключает фичу целиком."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.tool_call_detail import ToolCallDetailScreen
    from gc_tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._task_events = _tool_call_events()
        row = ToolCallRow(
            call_id="c1", tool_name="exec_command", args_summary="curl"
        )
        await scr.query_one("#trace").mount(row)
        row.set_result(summary="ok", error=False)
        await pilot.pause(0.05)

        assert not scr._confirm_active
        await _mouse_click(pilot, row, ctrl=True)

        assert any(
            isinstance(s, ToolCallDetailScreen) for s in pilot.app.screen_stack
        )


@pytest.mark.asyncio
async def test_confirm_ctrl_click_not_open_viewer_for_trace_row(tmp_path):
    """То же для обычной строки трейса (viewer-путь): Ctrl+клик при confirm
    не пушит ViewerScreen поверх баннера."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.models import TraceEvent
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.trace_row import TraceRow

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._task_events = [
            TraceEvent(
                no=5,
                event_ts_utc_iso="t",
                kind="agent",
                title="agent",
                summary="ответ",
                full_payload={"text": "ответ"},
            )
        ]
        row = TraceRow(
            prefix="● ", content="ответ", kind_class="line-agent", event_no=5
        )
        await scr.query_one("#trace").mount(row)
        await pilot.pause(0.05)

        scr.show_confirm_banner("Команда", "Разрешить?")
        await pilot.pause(0.05)
        stack_before = len(pilot.app.screen_stack)

        await _mouse_click(pilot, row, ctrl=True)

        assert len(pilot.app.screen_stack) == stack_before
        assert not any(
            isinstance(s, ViewerScreen) for s in pilot.app.screen_stack
        )
        assert scr._confirm_active

        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active


@pytest.mark.asyncio
async def test_confirm_click_does_not_toggle_summary_body(tmp_path):
    """SummaryMarker: клик при активном confirm не раскрывает тело сводки
    (симметрия с уже стоящим гейтом на Enter)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import SummaryMarker

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        marker = SummaryMarker(
            prefix="▸ ",
            content="──── контекст сжат ────",
            kind_class="summary-marker",
            event_no=9,
        )
        body = SummaryMarker(
            prefix="   ",
            content="Контекст сохранён:\nтекст",
            kind_class="summary-body hidden",
            event_no=9,
            focusable=False,
        )
        trace = scr.query_one("#trace")
        await trace.mount(marker)
        await trace.mount(body)
        scr._summary_marker_widgets[9] = marker
        scr._summary_body_widgets[9] = body
        await pilot.pause(0.05)

        scr.show_confirm_banner("Команда", "Разрешить?")
        await pilot.pause(0.05)
        assert body.has_class("hidden")

        await _mouse_click(pilot, marker)

        assert body.has_class("hidden")
        assert scr._confirm_active

        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == ["allow_remember"]
        assert not scr._confirm_active


@pytest.mark.asyncio
async def test_click_toggles_summary_body_without_confirm(tmp_path):
    """Контроль: без confirm клик по маркеру по-прежнему раскрывает сводку."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import SummaryMarker

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        marker = SummaryMarker(
            prefix="▸ ",
            content="──── контекст сжат ────",
            kind_class="summary-marker",
            event_no=9,
        )
        body = SummaryMarker(
            prefix="   ",
            content="Контекст сохранён:\nтекст",
            kind_class="summary-body hidden",
            event_no=9,
            focusable=False,
        )
        trace = scr.query_one("#trace")
        await trace.mount(marker)
        await trace.mount(body)
        scr._summary_marker_widgets[9] = marker
        scr._summary_body_widgets[9] = body
        await pilot.pause(0.05)

        assert not scr._confirm_active
        await _mouse_click(pilot, marker)

        assert not body.has_class("hidden")


@pytest.mark.asyncio
async def test_confirm_banner_receives_focus_on_show(tmp_path):
    """show_confirm_banner ставит фокус на баннер (не None / не tool-row)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from gc_tui.widgets.tool_call_row import ToolCallRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        row = ToolCallRow(
            call_id="c1", tool_name="exec_command", args_summary="x"
        )
        await scr.query_one("#trace").mount(row)
        row.focus()
        await pilot.pause(0.05)

        scr.show_confirm_banner("T", "Q?")
        await pilot.pause(0.05)

        assert isinstance(pilot.app.focused, ConfirmBanner)


# ── Tests for Ctrl+C / _reset_render_state confirm handling ──────────────────


@pytest.mark.asyncio
async def test_ctrl_c_during_init_style_confirm_cancels_and_hides_banner(
    tmp_path,
):
    """/init-стиль: _confirm_active=True, runner.is_running()==False.
    Ctrl+C → cancel_confirm вызван, баннер снят, run НЕ остановлен (не было живого)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from textual.containers import Horizontal

    runner = _StubRunner(running=False)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
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
        assert not pilot.app.query_one("#input-row", Horizontal).has_class(
            "hidden"
        )
        # run was NOT stopped (no live runner)
        assert not runner.stopped


@pytest.mark.asyncio
async def test_ctrl_c_during_live_agent_confirm_cancels_and_stops_run(
    tmp_path,
):
    """live-agent: _confirm_active=True, runner.is_running()==True.
    Ctrl+C → cancel_confirm вызван, баннер снят, раннер остановлен."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from textual.containers import Horizontal

    runner = _StubRunner(running=True)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
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
        assert not pilot.app.query_one("#input-row", Horizontal).has_class(
            "hidden"
        )
        # active_runner nulled by _stop_and_replace_runner
        assert pilot.app.active_runner is None


@pytest.mark.asyncio
async def test_reset_render_state_clears_confirm_banner(tmp_path):
    """Backstop: _reset_render_state при _confirm_active=True → баннер снят, cancel вызван."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.confirm_banner import ConfirmBanner
    from textual.containers import Horizontal

    runner = _StubRunner(running=False)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
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
        assert not pilot.app.query_one("#input-row", Horizontal).has_class(
            "hidden"
        )


@pytest.mark.asyncio
async def test_answered_banner_returns_a_following_trace_to_the_bottom(
    tmp_path,
):
    """Ответ снял баннер — лента, прижатая к низу, туда и возвращается."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)
        scrolled = []
        scr._scroll_trace_end = lambda: scrolled.append(True)
        scr._stick_bottom = True

        scr._answer_confirm("allow_once")
        scr._stick_bottom = False  # опрос увидел сдвиг низа после снятия
        await pilot.pause(0.1)

        assert runner.replies == ["allow_once"]
        assert scrolled  # опрос потом может прижать ещё раз
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_answered_banner_keeps_a_trace_scrolled_up_in_place(tmp_path):
    """Фокус уходит в поле ввода до снятия баннера: иначе Textual переводит
    его на фокусируемую строку ленты и прокручивает ленту к ней."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    class _Focusable(Static):
        can_focus = True

    runner = _StubRunner()
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"agent.model": "M", "backend.api_mode": "chat_completions"}
        ),
    )
    app.active_runner = runner

    async with app.run_test(size=(100, 30)) as pilot:
        scr = pilot.app.screen
        for i in range(80):
            scr._trace_mount(Static(f"строка {i}"))
        scr._trace_mount(_Focusable("строка с кнопкой"))
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.3)
        trace = scr.query_one("#trace", VerticalScroll)
        trace.scroll_to(y=10, animate=False)
        await pilot.pause(0.5)

        await pilot.press("o")
        await pilot.pause(0.5)

        assert runner.replies == ["allow_once"]
        assert trace.scroll_offset.y == 10
