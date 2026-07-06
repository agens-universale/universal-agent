"""Навигация по экранам: без задвоения (F1/F4), F2/F3/F4 — только с главного,
F1 (справка) — с любого экрана, кроме самой справки."""
import pytest


def _count(app, cls) -> int:
    return sum(1 for s in app.screen_stack if isinstance(s, cls))


@pytest.mark.asyncio
async def test_f4_notices_opens_once_no_stack(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.main import TraceScreen
    from tui.screens.notices import NoticesScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        await pilot.press("f4"); await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)
        # Повторные F4 на экране уведомлений — no-op (не стекаются).
        await pilot.press("f4"); await pilot.pause()
        await pilot.press("f4"); await pilot.pause()
        assert _count(pilot.app, NoticesScreen) == 1
        # Один Esc возвращает на главный.
        await pilot.press("escape"); await pilot.pause()
        assert isinstance(pilot.app.screen, TraceScreen)


@pytest.mark.asyncio
async def test_f1_help_opens_once_no_stack(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.help import HelpScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        await pilot.press("f1"); await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)
        await pilot.press("f1"); await pilot.pause()
        assert _count(pilot.app, HelpScreen) == 1


@pytest.mark.asyncio
async def test_f4_notices_noop_from_subscreen(tmp_path):
    """F4 (уведомления) — только с главного; с под-экрана (mcp) не открывается."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.mcp import McpScreen
    from tui.screens.notices import NoticesScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        pilot.app.screen._handle_command("mcp"); await pilot.pause()
        assert isinstance(pilot.app.screen, McpScreen)
        await pilot.press("f4"); await pilot.pause()
        assert _count(pilot.app, NoticesScreen) == 0
        assert isinstance(pilot.app.screen, McpScreen)


@pytest.mark.asyncio
async def test_f1_help_reachable_from_subscreen(tmp_path):
    """F1 (справка) доступна с любого экрана (тут — с mcp)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.mcp import McpScreen
    from tui.screens.help import HelpScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        pilot.app.screen._handle_command("mcp"); await pilot.pause()
        assert isinstance(pilot.app.screen, McpScreen)
        await pilot.press("f1"); await pilot.pause()
        assert isinstance(pilot.app.screen, HelpScreen)


@pytest.mark.asyncio
async def test_f2_f3_reachable_from_notices(tmp_path):
    """С экрана уведомлений можно перейти на config (F2) и mcp (F3)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.config import ConfigScreen
    from tui.screens.mcp import McpScreen
    from tui.screens.notices import NoticesScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        await pilot.press("f4"); await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)
        await pilot.press("f2"); await pilot.pause()
        assert isinstance(pilot.app.screen, ConfigScreen)
        await pilot.press("escape"); await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)  # config конечна → назад к notices
        await pilot.press("f3"); await pilot.pause()
        assert isinstance(pilot.app.screen, McpScreen)


@pytest.mark.asyncio
async def test_config_is_terminal_f3_noop(tmp_path):
    """config/mcp — конечные: с config F3 (mcp) не срабатывает (только F1 и Esc)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.config import ConfigScreen
    from tui.screens.mcp import McpScreen
    async with TuiApp(threads_dir=str(tmp_path), config={"model": "M"}).run_test() as pilot:
        pilot.app.screen._handle_command("config"); await pilot.pause()
        assert isinstance(pilot.app.screen, ConfigScreen)
        await pilot.press("f3"); await pilot.pause()
        assert _count(pilot.app, McpScreen) == 0
        assert isinstance(pilot.app.screen, ConfigScreen)
