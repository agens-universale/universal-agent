"""Empty-state — каноничный вид при запуске без задачи."""
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_empty_state_snapshot(snap_compare, tmp_path):
    from tui.app import TuiApp
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        config={"model": "CodeAgent", "backend_api_mode": "chat_completions"},
    )

    async def setup(pilot):
        await pilot.pause()
        # Фиксируем cwd для детерминированного футера в snapshot'е.
        pilot.app.tui_state.pending_cwd = "/work/project"
        pilot.app.screen._refresh_footer()
        await pilot.pause()

    assert snap_compare(app, terminal_size=(120, 32), run_before=setup)
