"""SkillPicker integration в TraceScreen: триггер, фильтр, mutex с командной палитрой."""
import pytest


class _FakeReg:
    def list_skills(self, *, pack=None, include_all=False):
        return [
            {"id": "supercoder:planner"},
            {"id": "supercoder:coder"},
            {"id": "codebase:using-codebase"},
            {"id": "skill-creator"},
        ]


def _patch_registry(monkeypatch):
    import tools.skills.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_skills_registry", lambda cfg=None: _FakeReg())


@pytest.mark.asyncio
async def test_picker_opens_on_skill_space(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1


@pytest.mark.asyncio
async def test_picker_filters_by_id_substring(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        rows_text = "\n".join(str(s.render()) for s in picker.query(".palette-row"))
        assert "supercoder:planner" in rows_text
        assert "supercoder:coder" in rows_text
        assert "codebase:using-codebase" not in rows_text


@pytest.mark.asyncio
async def test_picker_closes_when_space_removed(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker
    from tui.widgets.command_palette import CommandPalette

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1

        inp.text = "/skill"  # пробел убран → должна сработать командная палитра
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 0
        assert len(pilot.app.query(CommandPalette)) == 1


@pytest.mark.asyncio
async def test_palette_and_picker_mutually_exclusive(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker
    from tui.widgets.command_palette import CommandPalette

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        # Состояния: /skill -> palette; /skill " " -> picker; /skill -> palette снова
        inp.text = "/sk"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(SkillPicker)) == 0

        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 0
        assert len(pilot.app.query(SkillPicker)) == 1

        inp.text = "/skill"
        await pilot.pause(0.1)
        assert len(pilot.app.query(CommandPalette)) == 1
        assert len(pilot.app.query(SkillPicker)) == 0


@pytest.mark.asyncio
async def test_tab_completes_id_into_input_and_closes_picker(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        await pilot.press("tab")
        await pilot.pause(0.1)

        assert inp.text == "/skill supercoder:planner"
        assert len(pilot.app.query(SkillPicker)) == 0


@pytest.mark.asyncio
async def test_esc_closes_picker_keeps_input(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill super"
        await pilot.pause(0.1)
        await pilot.press("escape")
        await pilot.pause(0.1)

        assert len(pilot.app.query(SkillPicker)) == 0
        assert inp.text == "/skill super"


@pytest.mark.asyncio
async def test_empty_filter_result_shows_no_match_line(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    _patch_registry(monkeypatch)
    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill zzz_no_such"
        await pilot.pause(0.1)
        picker = pilot.app.query_one(SkillPicker)
        hint_text = "\n".join(str(s.render()) for s in picker.query(".palette-hint"))
        assert "нет совпадений" in hint_text
        # Нет .palette-row
        assert len(picker.query(".palette-row")) == 0


@pytest.mark.asyncio
async def test_skills_loaded_only_once_per_session(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    calls = {"n": 0}

    import tools.skills.registry as reg_mod

    def factory(cfg=None):
        calls["n"] += 1
        return _FakeReg()
    monkeypatch.setattr(reg_mod, "get_skills_registry", factory)

    from tui.app import TuiApp
    from tui.widgets.skill_picker import SkillPicker

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1
        inp.text = ""
        await pilot.pause(0.1)
        inp.text = "/skill "
        await pilot.pause(0.1)
        assert len(pilot.app.query(SkillPicker)) == 1
        # Реестр загружен один раз на всю сессию.
        assert calls["n"] == 1
