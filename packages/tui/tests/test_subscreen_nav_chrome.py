"""Круговая навигация ↑/↓ на подэкранах списков + убранный правый бренд-блок."""
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_skills_nav_wraps_around():
    pytest.importorskip("textual")
    from gc_tui.screens.skills import SkillsScreen
    from textual.app import App

    skills = [{"id": f"s{i}", "source": "system", "root_path": "/p"} for i in range(3)]

    class _A(App):
        def on_mount(self):
            self.push_screen(SkillsScreen(skills=skills))

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        assert scr._selected_idx == 0
        await pilot.press("up")            # с первой строки ↑ → на последнюю
        assert scr._selected_idx == 2
        await pilot.press("down")          # с последней ↓ → на первую
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_skills_header_has_no_brand_block():
    pytest.importorskip("textual")
    from gc_tui.screens.skills import SkillsScreen
    from textual.app import App

    class _A(App):
        def on_mount(self):
            self.push_screen(SkillsScreen(
                skills=[{"id": "s", "source": "system", "root_path": "/p"}]))

    async with _A().run_test() as pilot:
        assert len(pilot.app.screen.query("#header-right")) == 0


@pytest.mark.asyncio
async def test_sessions_nav_wraps_within_page(tmp_path):
    pytest.importorskip("textual")
    for i in range(3):
        tid = f"sess0000{i}"
        (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
            json.dumps({"thread_id": tid, "system": "cli",
                        "updated_at": f"2026-05-30T10:00:0{i}+00:00",
                        "messages_count": 1}), encoding="utf-8")
    from gc_tui.app import TuiApp
    from gc_tui.screens.sessions import SessionsScreen

    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        scr = pilot.app.screen
        assert scr._selected_idx == 0
        await pilot.press("up")            # с первой ↑ → на последнюю (в пределах страницы)
        assert scr._selected_idx == 2
        await pilot.press("down")          # с последней ↓ → на первую
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_mcp_header_has_no_brand_block():
    pytest.importorskip("textual")
    from gc_tui.screens.mcp import McpScreen
    from textual.app import App

    class _A(App):
        def on_mount(self):
            self.push_screen(McpScreen(tools_snapshot={}))

    async with _A().run_test() as pilot:
        assert len(pilot.app.screen.query("#header-right")) == 0
