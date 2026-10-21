"""Command palette overlay renders correctly when typing a slash command."""

from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


# Скиллы фиксированы тестом (спека P5 §5.6): палитра показывает их строками
# после команд; `pack:status` скрыт — имя занято командой /status.
_SKILLS = [
    {
        "id": "superpowers:systematic-debugging",
        "pack_id": "superpowers",
        "description": "Поиск причины ошибки по шагам",
    },
    {"id": "skill-creator", "description": "Создать новый скилл"},
    {"id": "pack:status", "pack_id": "pack", "description": "скрыт"},
]


class _Reg:
    async def list_skills(self, *, pack=None, include_all=False):
        return list(_SKILLS)


def test_palette_open_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_core.tools.skills import registry as reg_mod
    from gc_tui.app import TuiApp

    monkeypatch.setattr(
        reg_mod, "get_skills_registry", lambda cfg=None: _Reg()
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )

    async def setup(pilot):
        await pilot.pause()
        # Фиксируем cwd для детерминированного футера в snapshot'е.
        pilot.app.tui_state.pending_cwd = "/work/project"
        pilot.app.screen._refresh_footer()
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/s"  # triggers palette open with filter "s"
        await pilot.pause(0.2)

    assert snap_compare(app, terminal_size=(120, 24), run_before=setup)
