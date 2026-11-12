"""TraceScreen Esc confirm-hint в слоте футера (clear / stop); выход —
модалка."""

import pytest


@pytest.mark.asyncio
async def test_show_esc_hint_clear_swaps_footer_slot():
    """_show_esc_hint('clear') пишет в слоте футера текст подсказки."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_esc_hint("clear")
        await pilot.pause()
        text = str(pilot.app.query_one("#footer-slot").render())
        assert "Esc ещё раз, чтобы очистить" in text
        assert scr._esc_confirm_kind == "clear"


@pytest.mark.asyncio
async def test_show_esc_hint_stop_swaps_footer_slot():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_esc_hint("stop")
        await pilot.pause()
        assert "Esc ещё раз, чтобы остановить" in str(
            pilot.app.query_one("#footer-slot").render()
        )
        assert scr._esc_confirm_kind == "stop"


@pytest.mark.asyncio
async def test_clear_footer_hint_restores_footer_slot():
    """_clear_footer_hint возвращает бейдж в слоте футера и обнуляет
    state."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_esc_hint("clear")
        await pilot.pause()
        scr._clear_footer_hint()
        await pilot.pause()
        text = str(pilot.app.query_one("#footer-slot").render())
        assert "Esc ещё раз" not in text
        assert scr._esc_confirm_kind is None


@pytest.mark.asyncio
async def test_show_esc_hint_schedules_timer():
    """_show_esc_hint планирует timer (auto-reset через 2s)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_esc_hint("clear")
        await pilot.pause()
        assert scr._esc_confirm_kind == "clear"
        # Проверяем что таймер запланирован (не None после _show_esc_hint)
        assert scr._footer_hint_timer is not None


@pytest.mark.asyncio
async def test_esc_with_text_shows_clear_hint_first():
    """1-й Esc при тексте в input → подсказка 'очистить', input НЕ очищен."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "какой-то текст"
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind == "clear"
        # Input ещё с текстом
        assert pilot.app.query_one("#cmdinput").text == "какой-то текст"


@pytest.mark.asyncio
async def test_second_esc_in_window_clears_input():
    """2-й Esc до 2s → очищает input + сбрасывает hint."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "текст"
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.query_one("#cmdinput").text == ""
        assert pilot.app.screen._esc_confirm_kind is None


@pytest.mark.asyncio
async def test_any_key_after_esc_resets_hint():
    """Любая клавиша (не Esc) после 1-го Esc → сброс подсказки, действие НЕ выполняется."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app.query_one("#cmdinput").text = "x"
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind == "clear"
        await pilot.press("a")  # любая другая клавиша
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind is None
        # input не очищен
        assert "x" in pilot.app.query_one("#cmdinput").text


@pytest.mark.asyncio
async def test_esc_empty_input_no_run_opens_exit_modal():
    """1-й Esc при пустом input и без run → модалка выхода (не hint)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = None
        pilot.app.query_one("#cmdinput").text = ""
        await pilot.press("escape")
        await pilot.pause()
        modal = next(
            (
                s
                for s in pilot.app.screen_stack
                if isinstance(s, ConfirmScreen)
            ),
            None,
        )
        assert modal is not None
        assert modal.query_one("#confirm-dialog").border_title == "Выход"
        assert scr._esc_confirm_kind is None


@pytest.mark.asyncio
async def test_exit_modal_yes_calls_app_exit(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        called = {"flag": False}
        monkeypatch.setattr(
            pilot.app, "exit", lambda *a, **k: called.__setitem__("flag", True)
        )
        pilot.app.active_runner = None
        pilot.app.query_one("#cmdinput").text = ""
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert called["flag"] is True


@pytest.mark.asyncio
async def test_esc_during_active_run_with_events_shows_stop_hint():
    """1-й Esc при active run И ≥1 событии от агента → подсказка 'остановить'."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        pilot.app.active_runner = _FakeRunner()
        pilot.app.query_one("#cmdinput").text = ""
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind == "stop"


@pytest.mark.asyncio
async def test_esc_during_run_with_text_shows_clear_hint_not_stop():
    """Esc-лестница: текст в инпуте приоритетнее running — сначала очистка.

    1-й Esc при active run И тексте → подсказка 'очистить' (не 'остановить');
    2-й Esc чистит текст, run живёт. Безусловный стоп — /stop или Ctrl+C."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    stopped = {"n": 0}

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def stop(self) -> None:
            stopped.__setitem__("n", stopped["n"] + 1)

    async with TuiApp().run_test() as pilot:
        pilot.app.active_runner = _FakeRunner()
        pilot.app.query_one("#cmdinput").text = "недописанное сообщение"
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind == "clear"
        assert (
            pilot.app.query_one("#cmdinput").text == "недописанное сообщение"
        )
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.query_one("#cmdinput").text == ""
        assert stopped["n"] == 0  # run не тронут
        # Следующий Esc (пустое поле) — уже стоп-ветка.
        await pilot.press("escape")
        await pilot.pause()
        assert pilot.app.screen._esc_confirm_kind == "stop"


@pytest.mark.asyncio
async def test_esc_during_run_zero_events_with_text_clears_first():
    """Текст в инпуте отключает и мгновенный cancel-with-undo: сначала очистка."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def produced_output(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._last_user_message_id = "m_undo"
        scr._mount_user_reply("previous", state="pending")
        pilot.app.query_one("#cmdinput").text = "новый текст"
        await pilot.press("escape")
        await pilot.pause()
        assert scr._esc_confirm_kind == "clear"  # не мгновенная отмена
        assert pilot.app.query_one("#cmdinput").text == "новый текст"


@pytest.mark.asyncio
async def test_esc_during_active_run_zero_events_cancels_immediately():
    """1-й Esc при active run И 0 событий → cancel-with-undo сразу, без hint."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool:
            return True

        def produced_output(self) -> bool:
            return False

        def stop(self) -> None:
            pass

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.active_runner = _FakeRunner()
        scr._last_user_message_id = "m_undo"
        scr._mount_user_reply("previous", state="pending")
        pilot.app.query_one("#cmdinput").text = ""
        await pilot.press("escape")
        await pilot.pause()
        # Hint НЕ показывается
        assert scr._esc_confirm_kind is None
        # input восстановлен
        assert pilot.app.query_one("#cmdinput").text == "previous"
