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


_MD = (
    "# Заголовок\n\n"
    "Текст с **жирным** и `кодом`.\n\n"
    "- пункт\n\n"
    "| Роль | Язык |\n|---|---|\n| `Planner` | ru |\n\n"
    "```python\nprint(1)\n```\n"
)


@pytest.mark.asyncio
async def test_viewer_renders_markdown_like_the_feed():
    """Markdown — тем же движком, что ответ в ленте: разметка не видна,
    таблица и код — панелями."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen
    from gc_tui.widgets.block_panel import BlockPanel

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(
            ViewerScreen(title="skill · x", content=_MD, markdown=True)
        )
        await pilot.pause()
        scr = pilot.app.screen
        prose = " ".join(str(w.content) for w in scr.query(".trace-content"))
        assert "Заголовок" in prose and "жирным" in prose
        assert "#" not in prose and "**" not in prose and "`" not in prose
        assert "• пункт" in prose
        assert len(scr.query(BlockPanel)) == 2  # таблица и блок кода


@pytest.mark.asyncio
async def test_long_markdown_scrolls():
    """Длинное тело прокручивается: контейнер Markdown растёт по
    содержимому, а не обрезается по высоте окна."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    md = "\n\n".join(f"# h{i}\n\nтекст {i}" for i in range(60))
    async with TuiApp().run_test(size=(80, 30)) as pilot:
        pilot.app.push_screen(
            ViewerScreen(title="x", content=md, markdown=True)
        )
        await pilot.pause()
        assert pilot.app.screen.query_one("#trace").max_scroll_y > 0


@pytest.mark.asyncio
async def test_read_only_footer_is_ruled_off_from_the_body():
    """Подвал read-only экрана отделён полосой; у главного экрана — нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    async with TuiApp().run_test() as pilot:
        main_footer = pilot.app.screen.query_one("#footer")
        assert main_footer.styles.border_top[0] in ("", "none")
        pilot.app.push_screen(ViewerScreen(title="x", content="тело"))
        await pilot.pause()
        footer = pilot.app.screen.query_one("#footer")
        assert footer.styles.border_top[0] == "solid"
