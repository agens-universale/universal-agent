"""Command palette overlay renders correctly when typing a slash command."""
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_palette_open_snapshot(snap_compare, tmp_path):
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "CodeAgent", "backend_api_mode": "chat_completions"})

    async def setup(pilot):
        await pilot.pause()
        # Фиксируем cwd для детерминированного футера в snapshot'е.
        pilot.app.tui_state.pending_cwd = "/work/project"
        pilot.app.screen._refresh_footer()
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/s"   # triggers palette open with filter "s"
        await pilot.pause(0.2)

    assert snap_compare(app, terminal_size=(120, 24), run_before=setup)
