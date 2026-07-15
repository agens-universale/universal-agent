"""TUI: runtime LLM error → счётчик в карточке, футере и /notices."""
from __future__ import annotations

import pytest

from runtime_notices import clear_runtime_notices, push_runtime_notice
from startup_notices import Notice


@pytest.mark.asyncio
async def test_runtime_notice_appears_in_card(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    clear_runtime_notices()
    cfg = {"model": "M"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        assert "/notices" not in str(scr._mcp_status_row.render())

        push_runtime_notice(Notice(
            "warning", "llm_error_400",
            "Ошибка LLM (судья): HTTP 400. reasoning_effort invalid",
        ))
        pilot.app.refresh_notices_indicators()
        card = str(scr._mcp_status_row.render())
        assert "⚠ 1" in card
        assert "/notices (F4)" in card
        footer = str(scr.query_one("#footer-left").render())
        assert "⚠ 1" in footer
        assert "/notices (F4)" in footer

        notices = scr._collect_notices()
        assert any(n.code == "llm_error_400" for n in notices)


@pytest.mark.asyncio
async def test_runtime_notice_footer_on_sessions_disappears_after_view(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.notices import NoticesScreen
    from tui.screens.sessions import SessionsScreen

    clear_runtime_notices()
    async with TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"}).run_test() as pilot:
        push_runtime_notice(Notice("warning", "w1", "тест"))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SessionsScreen)
        footer_right = str(pilot.app.screen.query_one("#sessions-footer-right").render())
        assert "⚠ 1" in footer_right

        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SessionsScreen)
        footer_right = str(pilot.app.screen.query_one("#sessions-footer-right").render())
        assert "⚠" not in footer_right
        assert len(pilot.app.collect_notices()) == 1

        push_runtime_notice(Notice("warning", "w2", "тест2"))
        pilot.app.refresh_notices_indicators()
        footer_right = str(pilot.app.screen.query_one("#sessions-footer-right").render())
        assert "⚠ 1" in footer_right

