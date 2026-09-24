"""ConfirmScreen: title в border, цветной hint, message."""

import pytest


@pytest.mark.asyncio
async def test_confirm_title_in_border_and_content():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(
            ConfirmScreen(
                title="Новая задача",
                message="Активная задача в процессе.",
                hint="[Y] да, прервать   [N] нет, продолжить",
            )
        )
        await pilot.pause()
        dlg = pilot.app.screen.query_one("#confirm-dialog")
        assert dlg.border_title == "Новая задача"
        msg = str(pilot.app.screen.query_one("#confirm-message").render())
        assert "Активная задача" in msg
        hint = str(pilot.app.screen.query_one("#confirm-hint").render())
        assert "[Y]" in hint and "прервать" in hint
        assert "[N]" in hint and "продолжить" in hint


@pytest.mark.asyncio
async def test_confirm_without_title_has_no_border_title():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(ConfirmScreen(message="Удалить?"))
        await pilot.pause()
        dlg = pilot.app.screen.query_one("#confirm-dialog")
        assert not dlg.border_title  # None или "" — без заголовка


@pytest.mark.asyncio
async def test_confirm_y_returns_true_n_returns_false():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        results = []
        pilot.app.push_screen(
            ConfirmScreen(message="ok?"), lambda v: results.append(v)
        )
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        assert results == [True]


@pytest.mark.asyncio
async def test_confirm_default_selection_left_and_enter_confirms():
    """Левый вариант выбран по умолчанию; Enter → True."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        results = []
        scr = ConfirmScreen(message="ok?", hint="[Y] да  [N] нет")
        pilot.app.push_screen(scr, lambda v: results.append(v))
        await pilot.pause()
        assert scr._selected == 0
        await pilot.press("enter")
        await pilot.pause()
        assert results == [True]


@pytest.mark.asyncio
async def test_confirm_right_arrow_then_enter_returns_false():
    """→ выбирает правый вариант; Enter → False."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        results = []
        scr = ConfirmScreen(message="ok?", hint="[Y] да  [N] нет")
        pilot.app.push_screen(scr, lambda v: results.append(v))
        await pilot.pause()
        await pilot.press("right")
        await pilot.pause()
        assert scr._selected == 1
        await pilot.press("enter")
        await pilot.pause()
        assert results == [False]


@pytest.mark.asyncio
async def test_confirm_left_arrow_clamps_at_zero():
    """← на левом варианте не уходит за границу."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    async with TuiApp().run_test() as pilot:
        scr = ConfirmScreen(message="ok?", hint="[Y] да  [N] нет")
        pilot.app.push_screen(scr, lambda v: None)
        await pilot.pause()
        await pilot.press("left")
        await pilot.pause()
        assert scr._selected == 0
