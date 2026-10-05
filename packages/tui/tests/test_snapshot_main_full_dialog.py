"""Main-screen с полным диалогом: user → tool OK → tool ERR → submit_solution с comment."""

from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.tui_fixtures.stub_executor import StubExecutor


def test_main_full_dialog_snapshot(snap_compare, tmp_path):
    from gc_tui.app import TuiApp
    from gc_tui.services.stores.file_store import FileTaskStore

    tid = "7f3a9b1c"
    fake = StubExecutor(
        threads_dir=str(tmp_path), system="cli", scenario="full_dialog"
    )
    fake.run(["расскажи об этом проекте"], thread_id=tid)
    # Фикс cwd → детерминированный футер в снапшоте.
    FileTaskStore(threads_dir=str(tmp_path), system="cli").update_tui_meta(
        tid, {"cwd": "/work/project"}
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
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(120, 40), run_before=setup)
