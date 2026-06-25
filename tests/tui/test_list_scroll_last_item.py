"""Баг-фикс: при навигации стрелками по спискам скиллов/сессий прокручиваются
ОБЕ строки карточки (заголовок + мета), и заголовок (верх элемента) — якорь.

Раньше скроллился только заголовок → нижняя строка последнего элемента
оставалась под сгибом. После первого фикса (скроллим обе) порядок «заголовок,
потом мета» давал зеркальный баг: при переходе вниз-с-последнего-на-первый видна
только нижняя строка первого, а заголовок уезжал над сгибом.

Контракт (детерминированно, без зависимости от реальной геометрии): при выборе
элемента `scroll_visible` зовётся на ОБОИХ его виджетах, причём на МЕТЕ раньше,
чем на ЗАГОЛОВКЕ (заголовок скроллим последним → верх элемента «выигрывает»
позицию; 2-строчный элемент влезает в окно, поэтому мета под ним тоже видна).
"""
import json
from pathlib import Path

import pytest

_SKILLS = [
    {"id": "a-skill", "source": "system", "root_path": "/sys/a-skill"},
    {"id": "b-skill", "source": "user", "root_path": "/usr/b-skill"},
    {"id": "c-skill", "source": "project", "root_path": "/proj/c-skill"},
]


def _assert_both_scrolled_meta_first(scrolled, title, meta):
    assert title in scrolled and meta in scrolled        # прокручены обе строки
    assert scrolled.index(meta) < scrolled.index(title)  # заголовок — последним (якорь)


@pytest.mark.asyncio
async def test_skills_wrap_to_first_scrolls_both_title_anchored(monkeypatch):
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
        await pilot.press("end")              # на последний
        await pilot.pause()
        scrolled.clear()
        await pilot.press("down")             # вниз → круговой переход на ПЕРВЫЙ (репро)
        await pilot.pause()
        await pilot.pause()
        title = list(listview.query(".card-title"))[0]
        meta = list(listview.query(".card-meta"))[0]
        _assert_both_scrolled_meta_first(scrolled, title, meta)


def _seed_session(threads_dir, i):
    tid = f"{i:032d}"
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": f"2026-05-{i + 1:02d}T10:00:00+00:00",
                    "messages_count": 1}), encoding="utf-8")


@pytest.mark.asyncio
async def test_sessions_wrap_to_first_scrolls_both_title_anchored(tmp_path, monkeypatch):
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
        await pilot.press("end")
        await pilot.pause()
        scrolled.clear()
        await pilot.press("down")
        await pilot.pause()
        await pilot.pause()
        title = list(listview.query(".card-title"))[0]
        meta = list(listview.query(".card-meta"))[0]
        _assert_both_scrolled_meta_first(scrolled, title, meta)
