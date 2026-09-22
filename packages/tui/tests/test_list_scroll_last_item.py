"""Списки скиллов/сессий: каждый элемент — ОДИН виджет (.card, 2 строки), как
карточка MCP. Тогда `scroll_visible` тащит элемент целиком в ОБОИХ направлениях
кругового перехода — нет конфликта двух независимых прокруток (заголовок vs мета),
из-за которого то нижняя, то верхняя строка пряталась под сгибом.
"""
import json
from pathlib import Path

import pytest

_SKILLS = [
    {"id": "a-skill", "source": "system", "root_path": "/sys/a-skill"},
    {"id": "b-skill", "source": "user", "root_path": "/usr/b-skill"},
    {"id": "c-skill", "source": "project", "root_path": "/proj/c-skill"},
]


@pytest.mark.asyncio
async def test_skills_item_single_card_scrolls_both_wraps(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.skills import SkillsScreen
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    scrolled: list = []
    monkeypatch.setattr(Static, "scroll_visible", lambda self, *a, **k: scrolled.append(self))

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        listview = pilot.app.screen.query_one("#skills-list", VerticalScroll)
        # один виджет на элемент (а не два) → scroll_visible тащит обе строки разом
        assert len(listview.query(".card")) == len(_SKILLS)
        await pilot.press("end")
        await pilot.pause()
        scrolled.clear()
        await pilot.press("down")               # круговой переход вниз → первый
        await pilot.pause()
        await pilot.pause()
        assert list(listview.query(".card"))[0] in scrolled
        scrolled.clear()
        await pilot.press("up")                 # круговой переход вверх → последний
        await pilot.pause()
        await pilot.pause()
        assert list(listview.query(".card"))[-1] in scrolled


def _seed_session(threads_dir, i):
    tid = f"{i:032d}"
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": f"2026-05-{i + 1:02d}T10:00:00+00:00",
                    "messages_count": 1}), encoding="utf-8")


@pytest.mark.asyncio
async def test_sessions_item_single_card_scrolls_both_wraps(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.sessions import SessionsScreen
    from textual.containers import VerticalScroll
    from textual.widgets import Static

    for i in range(5):
        _seed_session(tmp_path, i)

    scrolled: list = []
    monkeypatch.setattr(Static, "scroll_visible", lambda self, *a, **k: scrolled.append(self))

    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        listview = pilot.app.screen.query_one("#sessions-list", VerticalScroll)
        assert len(listview.query(".card")) == 5
        await pilot.press("end")
        await pilot.pause()
        scrolled.clear()
        await pilot.press("down")
        await pilot.pause()
        await pilot.pause()
        assert list(listview.query(".card"))[0] in scrolled
        scrolled.clear()
        await pilot.press("up")
        await pilot.pause()
        await pilot.pause()
        assert list(listview.query(".card"))[-1] in scrolled
