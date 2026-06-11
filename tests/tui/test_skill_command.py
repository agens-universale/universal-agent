"""/skills загружает реестр; /skill <id> открывает описание в Viewer."""
import pytest


class _FakeReg:
    def list_skills(self, *, pack=None, include_all=False):
        return [
            {"id": "supercoder", "name": "supercoder", "description": "supercoder pack"},
            {"id": "codebase", "name": "codebase", "description": "codebase pack"},
        ]


@pytest.mark.asyncio
async def test_skills_command_lists_from_registry(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    import tools.skills.registry as reg_mod
    monkeypatch.setattr(reg_mod, "get_skills_registry", lambda cfg=None: _FakeReg())

    from textual.widgets import Static
    from tui.app import TuiApp
    from tui.screens.skills import SkillsScreen

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/skills"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert isinstance(pilot.app.screen, SkillsScreen)
        text = "\n".join(str(s.render()) for s in pilot.app.screen.query(Static))
        assert "supercoder" in text
        assert "codebase" in text
        assert "недоступны" not in text


