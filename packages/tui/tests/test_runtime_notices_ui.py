"""TUI: runtime LLM error → счётчик в футере и /notices (шапку не трогает)."""

from __future__ import annotations

from gc_testkit.tui_support import tui_settings
import pytest
from gc_core.runtime_notices import clear_runtime_notices, push_runtime_notice
from gc_core.startup_notices import Notice
from gc_tui.services.notices_ui import (
    WARN_GLYPH,
)  # в значке вариационный селектор


@pytest.mark.asyncio
async def test_runtime_notice_appears_in_footer_not_header(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    clear_runtime_notices()
    cfg = {"agent.model": "M"}
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), settings=tui_settings(cfg)
    ).run_test(size=(120, 40)) as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        header = scr.query_one("#brand-block")

        def header_text() -> str:
            width = header.content_region.width
            return header.render().laid_out(width).plain

        before = header_text()
        push_runtime_notice(
            Notice(
                "warning",
                "llm_error_400",
                "Ошибка LLM (судья): HTTP 400. reasoning_effort invalid",
            )
        )
        pilot.app.refresh_notices_indicators()
        await pilot.pause()
        assert header_text() == before  # шапка уведомлений не показывает
        assert "/notices" not in before
        footer = str(scr.query_one("#footer-slot").render())
        assert f"{WARN_GLYPH} 1" in footer
        assert "F4/notices" in footer

        notices = scr._collect_notices()
        assert any(n.code == "llm_error_400" for n in notices)


@pytest.mark.asyncio
async def test_runtime_notice_footer_on_sessions_disappears_after_view(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.notices import NoticesScreen
    from gc_tui.screens.sessions import SessionsScreen

    clear_runtime_notices()
    async with TuiApp(
        threads_dir=str(tmp_path / "t"),
        settings=tui_settings({"agent.model": "M"}),
    ).run_test() as pilot:
        push_runtime_notice(Notice("warning", "w1", "тест"))
        await pilot.press("ctrl+s")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SessionsScreen)
        footer_right = str(
            pilot.app.screen.query_one("#sessions-footer-right").render()
        )
        assert f"{WARN_GLYPH} 1" in footer_right

        await pilot.press("f4")
        await pilot.pause()
        assert isinstance(pilot.app.screen, NoticesScreen)

        await pilot.press("escape")
        await pilot.pause()
        assert isinstance(pilot.app.screen, SessionsScreen)
        footer_right = str(
            pilot.app.screen.query_one("#sessions-footer-right").render()
        )
        assert "⚠" not in footer_right
        assert len(pilot.app.collect_notices()) == 1

        push_runtime_notice(Notice("warning", "w2", "тест2"))
        pilot.app.refresh_notices_indicators()
        footer_right = str(
            pilot.app.screen.query_one("#sessions-footer-right").render()
        )
        assert f"{WARN_GLYPH} 1" in footer_right
