"""Main-screen с полным диалогом: user → tool OK → tool ERR → submit_solution с comment."""

from gc_testkit.tui_support import tui_settings
import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_testkit.trace_grid import assert_trace_grid, trace_rows
from gc_testkit.tui_fixtures.stub_executor import StubExecutor


def test_main_full_dialog_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.services.stores.file_store import FileTaskStore
    from gc_tui.widgets.turn_line import TurnLine

    # Папку в шапке фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )

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
        # Мигание курсора поля — таймер 0.5 с, фаза в кадре от нагрузки;
        # без мигания курсор виден всегда (приём `test_snapshot_forms`).
        pilot.app.screen.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)
        rows = trace_rows(pilot.app, exclude=(TurnLine,))
        start = next(
            i for i, r in enumerate(rows) if r.plain[1:2] in ("›", "●")
        )
        assert_trace_grid(rows[start:], origin=1)

    assert snap_compare(app, terminal_size=(120, 40), run_before=setup)
