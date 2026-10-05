"""Интеракция карточек /mcp: фокус ↑↓, toggle Enter/Space/клик (spec 2026-06-10)."""

from gc_testkit.tui_support import as_settings, tui_settings
import pytest

SNAP = {
    "office": {
        "status": "connected",
        "reason": None,
        "description": "Read office docs",
        "tools": [
            {"name": "mcp_office_read_file", "description": "Читает документ"}
        ],
    },
    "computer": {
        "status": "failed",
        "reason": "команда не найдена",
        "description": "GUI control",
        "tools": [],
    },
}


async def _open(tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"mcp.servers": {}}),
    )
    scr = McpScreen(settings=as_settings({}), tools_snapshot=dict(SNAP))
    return app, scr


@pytest.mark.asyncio
async def test_first_card_focused_on_mount(tmp_path):
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        assert app.focused is scr.cards[0]


@pytest.mark.asyncio
async def test_arrows_move_focus_between_cards(tmp_path):
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down")
        assert app.focused is scr.cards[1]
        await pilot.press("up")
        assert app.focused is scr.cards[0]


@pytest.mark.asyncio
async def test_enter_and_space_toggle_focused_card(tmp_path):
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        assert scr.cards[0].expanded is True
        assert "read_file" in scr.rendered_text()
        await pilot.press("space")
        assert scr.cards[0].expanded is False


@pytest.mark.asyncio
async def test_click_toggles_card(tmp_path):
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.click(scr.cards[1])
        assert scr.cards[1].expanded is True
        assert app.focused is scr.cards[1]  # клик и фокусирует тоже


@pytest.mark.asyncio
async def test_trace_container_not_focusable(tmp_path):
    # Фикс outline-глюка: контейнер не берёт фокус — глобальный
    # #trace:focus-outline на этом экране не срабатывает.
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        assert scr.query_one("#trace").can_focus is False


@pytest.mark.asyncio
async def test_expanded_failed_card_shows_full_reason(tmp_path):
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down", "enter")
        assert scr.cards[1].expanded is True
        assert "команда не найдена" in scr.rendered_text()


@pytest.mark.asyncio
async def test_arrows_move_focus_when_content_overflows(tmp_path):
    # Регрессия: биндинги VerticalScroll не должны съедать ↑↓ при переполнении.
    pytest.importorskip("textual")
    app, scr = await _open(tmp_path)
    async with app.run_test(size=(80, 8)) as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")  # раскрыли первую карточку
        assert scr.cards[0].expanded is True
        await pilot.press("down")
        assert app.focused is scr.cards[1]
        await pilot.press("up")
        assert app.focused is scr.cards[0]


@pytest.mark.asyncio
async def test_keys_on_empty_screen_do_not_crash(tmp_path):
    # Нет карточек (пустой снапшот) — клавиши не должны ронять экран.
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.mcp import McpScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"mcp.servers": {}}),
    )
    scr = McpScreen(settings=as_settings({}), tools_snapshot={})
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("down", "up", "enter", "pagedown", "pageup")
        assert "нет MCP-серверов" in scr.rendered_text()
