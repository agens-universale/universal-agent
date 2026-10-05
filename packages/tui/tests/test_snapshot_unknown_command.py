from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_unknown_command_snapshot(snap_compare, tmp_path):
    from gc_tui.app import TuiApp

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
        # Фиксируем cwd для детерминированного футера в snapshot'е.
        pilot.app.tui_state.pending_cwd = "/work/project"
        pilot.app.screen._refresh_footer()
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/tst"
        await pilot.press("enter")
        await pilot.pause(0.1)

    assert snap_compare(app, terminal_size=(120, 24), run_before=setup)
