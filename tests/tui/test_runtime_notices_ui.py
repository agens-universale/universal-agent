"""TUI: runtime LLM error → счётчик в карточке и /notices."""
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
        assert "уведомления:" not in str(scr._mcp_status_row.render())

        push_runtime_notice(Notice(
            "warning", "llm_error_400",
            "Ошибка LLM (судья): HTTP 400. reasoning_effort invalid",
        ))
        scr._refresh_notices_card()
        card = str(scr._mcp_status_row.render())
        assert "уведомления:" in card
        assert "/notices (F4)" in card

        notices = scr._collect_notices()
        assert any(n.code == "llm_error_400" for n in notices)
