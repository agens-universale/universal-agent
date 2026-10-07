"""SkillsScreen / ViewerScreen / HelpScreen — открытие, контент."""

import pytest


@pytest.mark.asyncio
async def test_help_screen_lists_commands_from_registry():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/help"
        await pilot.press("enter")
        await pilot.pause()
        rows = pilot.app.screen.query(".help-row-cmd")
        names = "\n".join(str(r.render()) for r in rows)
        for n in [
            "/sessions",
            "/stop",
            "/skills",
            "/quit",
            "/notices",
            "/diff",
            "/view",
        ]:
            assert n in names


@pytest.mark.asyncio
async def test_viewer_screen_shows_content():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(
            ViewerScreen(
                title="test.txt",
                content="строка1\nстрока2\n",
            )
        )
        await pilot.pause()
        body = pilot.app.screen.query_one(".viewer-body")
        assert "строка1" in str(body.render())


@pytest.mark.asyncio
async def test_viewer_header_shows_full_title_no_version():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen
    from textual.widgets import Static

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(
            ViewerScreen(title="skill · x · системный · /p/x", content="тело")
        )
        await pilot.pause()
        left = str(pilot.app.screen.query_one("#header-left", Static).render())
        assert "skill · x · системный · /p/x" in left
        # «F1 help · gc» убран из шапки вьюера — больше места под путь
        assert len(pilot.app.screen.query("#header-right")) == 0
