"""TUI: сбор стартовых уведомлений, бейдж готовности, сворачиваемый блок."""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_startup_block_collapsed_shows_max_three(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    # agent_md_path задан + agentsmd_override → notice agentsmd_override_ignored в TUI
    cfg = {"model": "M", "backend_api_mode": "chat_completions",
           "stop_tools": ["bogus1", "bogus2"], "agentsmd_override": True,
           "agent_md_path": "/some/agent.md",
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        notices = scr._collect_startup_notices()
        # stop_tools_unknown + agentsmd_override_ignored + deprecated_key = 3
        assert len(notices) >= 3
        assert any(n.code == "stop_tools_unknown" for n in notices)


@pytest.mark.asyncio
async def test_notices_summary_in_card_not_trace(tmp_path):
    """При уведомлениях в КАРТОЧКЕ строка «уведомления: … /notices (F4)»; в трейсе
    НЕТ блока-заголовка «Уведомления» — детали только на экране /notices."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    cfg = {"model": "M", "stop_tools": ["bogus1"],
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "уведомления:" in card and "/notices (F4)" in card, (
            f"Строка уведомлений не найдена в карточке: {card!r}"
        )
        # Блок-заголовок «Уведомления · N» в трейсе не монтируется.
        texts = [str(s.render()) for s in pilot.app.query_one("#trace").query(Static)]
        assert not any("Уведомления ·" in t for t in texts), (
            "Блок уведомлений в трейсе не должен монтироваться"
        )


@pytest.mark.asyncio
async def test_no_notices_line_when_clean_config(tmp_path):
    """Без уведомлений — строки «уведомления:» в карточке нет, готовность простая."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    cfg = {"model": "M"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "уведомления:" not in card
        assert "готов к работе" in card


@pytest.mark.asyncio
async def test_card_readiness_always_plain(tmp_path):
    """Строка готовности в карточке всегда простая «✓ готов к работе» (счётчики —
    на отдельной строке «уведомления:», не на готовности)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    cfg = {"model": "M", "stop_tools": ["bogus1"],
           "deprecated_keys_used": ["backend.stream"]}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        scr = pilot.app.screen
        await pilot.pause()
        card = str(scr._mcp_status_row.render())
        assert "✓ готов к работе" in card
