"""Баг-фикс: при выборе ПОСЛЕДНЕГО элемента стрелками прокручиваются ОБЕ строки
карточки (заголовок + мета). Раньше скроллился только заголовок → вторая строка
последнего скилла/сессии оставалась под сгибом, пока не доскроллишь мышью.

Контракт фикса (детерминированно, без зависимости от реальной геометрии): при
выборе элемента `scroll_visible` вызывается на ОБОИХ его виджетах — `.card-title`
и `.card-meta`.
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
async def test_skills_last_item_both_lines_scrolled(monkeypatch):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen

    scrolled: list = []
    monkeypatch.setattr(Static, "scroll_visible",
                        lambda self, *a, **k: scrolled.append(self))

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        listview = pilot.app.screen.query_one("#skills-list", VerticalScroll)
        scrolled.clear()
        await pilot.press("end")                 # выбрать последний
        await pilot.pause()
        await pilot.pause()
        title = list(listview.query(".card-title"))[-1]
        meta = list(listview.query(".card-meta"))[-1]
        assert title in scrolled                 # заголовок (как и раньше)
        assert meta in scrolled                  # и мета (фикс) — иначе 2-я строка скрыта


def _seed_session(threads_dir, i):
    tid = f"{i:032d}"
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": f"2026-05-{i + 1:02d}T10:00:00+00:00",
                    "messages_count": 1}), encoding="utf-8")


@pytest.mark.asyncio
async def test_sessions_last_item_both_lines_scrolled(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.sessions import SessionsScreen

    for i in range(5):
        _seed_session(tmp_path, i)

    scrolled: list = []
    monkeypatch.setattr(Static, "scroll_visible",
                        lambda self, *a, **k: scrolled.append(self))

    app = TuiApp(threads_dir=str(tmp_path), system="cli")
    async with app.run_test() as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        listview = pilot.app.screen.query_one("#sessions-list", VerticalScroll)
        scrolled.clear()
        await pilot.press("end")
        await pilot.pause()
        await pilot.pause()
        title = list(listview.query(".card-title"))[-1]
        meta = list(listview.query(".card-meta"))[-1]
        assert title in scrolled
        assert meta in scrolled
