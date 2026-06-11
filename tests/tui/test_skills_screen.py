"""SkillsScreen picker: колонки, навигация, фильтр, Enter→viewer."""
import pytest

_SKILLS = [
    {"id": "articles:article-creator", "source": "system", "root_path": "/sys/articles/article-creator"},
    {"id": "hello-world", "source": "user", "root_path": "/usr/hello-world"},
    {"id": "react-agent-analyzer", "source": "project", "root_path": "/proj/react-agent-analyzer"},
]


@pytest.mark.asyncio
async def test_rows_show_id_type_path():
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        rows = "\n".join(
            str(s.render())
            for s in pilot.app.screen.query_one("#skills-list", VerticalScroll).query("Static")
        )
        assert "articles:article-creator" in rows
        assert "системный" in rows
        assert "/sys/articles/article-creator" in rows
        assert "пользовательский" in rows
        assert "проектный" in rows


@pytest.mark.asyncio
async def test_arrow_keys_move_selection():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        assert scr._selected_idx == 0
        await pilot.press("down")
        assert scr._selected_idx == 1
        await pilot.press("up")
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_home_end_jump_selection():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        assert scr._selected_idx == 0
        await pilot.press("end")
        assert scr._selected_idx == len(_SKILLS) - 1
        await pilot.press("home")
        assert scr._selected_idx == 0


@pytest.mark.asyncio
async def test_filter_narrows_by_id_and_path():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        scr.query_one("#skills-filter").value = "hello"
        await pilot.pause()
        assert [s["id"] for s in scr._skills] == ["hello-world"]
        # фильтр по пути:
        scr.query_one("#skills-filter").value = "proj"
        await pilot.pause()
        assert [s["id"] for s in scr._skills] == ["react-agent-analyzer"]


@pytest.mark.asyncio
async def test_enter_opens_skill_viewer(monkeypatch):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    from tui.screens.viewer import ViewerScreen

    class _Reg:
        def load_skill_detail(self, sid):
            return {"id": sid, "name": sid, "source": "user",
                    "root_path": "/usr/" + sid, "description": "d", "instructions": "i"}

    monkeypatch.setattr("tools.skills.registry.get_skills_registry", lambda cfg: _Reg())

    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS, config={"model": "M"}))
        await pilot.pause()
        await pilot.press("down")   # выбрать hello-world
        await pilot.press("enter")
        await pilot.pause()
        assert isinstance(pilot.app.screen, ViewerScreen)
        left = str(pilot.app.screen.query_one("#header-left", Static).render())
        assert "skill · hello-world" in left
        assert "пользовательский" in left


@pytest.mark.asyncio
async def test_two_line_card_structure_and_count():
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        listview = scr.query_one("#skills-list", VerticalScroll)
        assert len(listview.query("Static")) == 2 * len(_SKILLS)
        titles = list(listview.query(".card-title"))
        metas = list(listview.query(".card-meta"))
        assert len(titles) == len(_SKILLS)
        assert len(metas) == len(_SKILLS)
        assert "articles:article-creator" in str(titles[0].render())
        assert "системный" in str(metas[0].render())
        header = str(scr.query_one("#header-left", Static).render())
        assert "доступные навыки · 3" in header


@pytest.mark.asyncio
async def test_filter_by_type_label():
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test() as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        scr.query_one("#skills-filter").value = "проек"
        await pilot.pause()
        assert [s["id"] for s in scr._skills] == ["react-agent-analyzer"]


@pytest.mark.asyncio
async def test_skills_list_fills_screen_height():
    pytest.importorskip("textual")
    from textual.containers import VerticalScroll
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen
    async with TuiApp().run_test(size=(80, 30)) as pilot:
        pilot.app.push_screen(SkillsScreen(skills=_SKILLS))
        await pilot.pause()
        scr = pilot.app.screen
        listview = scr.query_one("#skills-list", VerticalScroll)
        # Список должен растягиваться почти на всю высоту (экран − шапка − фильтр − футер),
        # а не схлопываться по контенту (3 скилла) / занимать половину.
        assert listview.size.height >= pilot.app.size.height - 8
