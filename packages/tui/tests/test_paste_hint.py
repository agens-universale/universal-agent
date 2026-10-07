"""Pilot-тест: сворачивание вставки показывает footer-подсказку."""

from unittest.mock import patch

import pytest


@pytest.mark.asyncio
async def test_paste_collapse_shows_footer_hint():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.screen.query_one("#cmdinput", CommandTextArea)
        inp._handle_paste("a\n" * 4 + "a")  # 5 строк → сворачивается
        await pilot.pause()
        left = pilot.app.screen.query_one("#footer-left", Static)
        assert "развернуть" in str(left.render())


@pytest.mark.asyncio
async def test_paste_hint_lasts_5_seconds():
    """Подсказка повторной вставки держится 5с (а не 2с, как раньше)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        with patch.object(scr, "set_timer", wraps=scr.set_timer) as spy:
            scr.query_one("#cmdinput", CommandTextArea)._handle_paste(
                "a\n" * 4 + "a"
            )
            await pilot.pause()
        delays = [c.args[0] for c in spy.call_args_list]
        assert 5.0 in delays


@pytest.mark.asyncio
async def test_footer_refusal_yields_to_the_next_keystroke():
    """Отказ читается, пока человек бездействует; первая же следующая клавиша
    возвращает футеру актуальные команды — не ждать 5 секунд (живой смок
    владельца 2026-09-04, то же правило, что у esc-confirm)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause()
        left = pilot.app.screen.query_one("#footer-left", Static)
        assert "не сохранено" in str(left.render())
        await pilot.press("x")
        assert "не сохранено" not in str(left.render())


@pytest.mark.asyncio
async def test_footer_refusal_yields_to_a_running_task():
    """Запущенный прогон вытесняет подсказку: футер обязан показать
    «Ctrl+C/stop», а не дожёвывать 5-секундный отказ."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    class _Runner:
        def is_running(self):
            return True

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause()
        pilot.app.tui_state.active_task_id = "t1"
        pilot.app.active_runner = _Runner()
        scr._refresh_footer()
        left = pilot.app.screen.query_one("#footer-left", Static)
        assert "не сохранено" not in str(left.render())
        assert "Ctrl+C/stop" in str(left.render())


@pytest.mark.asyncio
async def test_footer_refusal_survives_the_deferred_input_clear():
    """Живой порядок команды: `inp.text = ""` стоит ДО исполнения, но Changed
    от него доставляется ПОСЛЕ — когда отказ уже показан. Пустое программное
    изменение не должно гасить отказ в момент появления (смок владельца
    2026-09-04: подсказка «не сохранено» не появлялась вовсе); первая же
    человеческая печать — гасит."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        inp = scr.query_one("#cmdinput", CommandTextArea)
        inp.text = "/profiles"
        await pilot.pause()
        # порядок обработчика команды: очистка → исполнение → отказ; Changed("")
        # ещё в очереди, когда отказ уже активен
        inp.text = ""
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause()
        left = scr.query_one("#footer-left", Static)
        assert "не сохранено" in str(
            left.render()
        )  # пережил отложенный Changed
        await pilot.press("q")
        assert "не сохранено" not in str(left.render())


@pytest.mark.asyncio
async def test_ordinary_hint_after_a_refusal_keeps_its_own_canon():
    """Подсказка, показанная ПОСЛЕ отказа, живёт по СВОЕМУ канону: режим
    «гасить печатью» принадлежит отказу, а не залипшему флагу — иначе
    paste-hint исчезал бы от первой же буквы."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause()
        scr._show_footer_hint("обычная подсказка", seconds=5.0)
        await pilot.pause()
        left = scr.query_one("#footer-left", Static)
        await pilot.press("q")
        assert "обычная подсказка" in str(left.render())  # печать её не гасит


@pytest.mark.asyncio
async def test_refusal_raised_during_a_run_survives_the_poll_tick():
    """Отказ, поставленный ВО ВРЕМЯ прогона (/gate из палитры, пока агент
    работает), не гасится тиком опроса: «прогон вытесняет отказ» — только про
    прогон, НАЧАВШИЙСЯ после постановки (ревью 2026-09-04, находка 1 — иначе
    5 секунд не читались никогда именно там, где отказы и жмут)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    class _Runner:
        def is_running(self):
            return True

    async with TuiApp().run_test() as pilot:
        scr = pilot.app.screen
        pilot.app.tui_state.active_task_id = "t1"
        pilot.app.active_runner = _Runner()
        scr._show_footer_refusal("✗ не сохранено: причина")
        await pilot.pause()
        scr._refresh_footer()  # тик поллера при ТОМ ЖЕ прогоне
        left = scr.query_one("#footer-left", Static)
        assert "не сохранено" in str(left.render())
        pilot.app.tui_state.active_task_id = "t2"  # НОВЫЙ прогон — уступить
        scr._refresh_footer()
        assert "не сохранено" not in str(left.render())
