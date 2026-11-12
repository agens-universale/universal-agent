"""При meta.runtime.summarizing=True индикатор переходит в compressing."""

from gc_testkit.develop_files import record_from_meta

from gc_testkit.tui_support import tui_settings
import pytest


class _RunningRunner:
    def is_running(self):
        return True

    def stop_run(self):
        pass

    def add_message(self, m):
        pass


class _FakeStore:
    def get_record(self, task_id, system=None):
        return record_from_meta({"runtime": {"summarizing": True}})

    def update_tui_meta(self, task_id, patch):
        pass

    def tail_events(self, task_id, cursor):
        return type(
            "T",
            (),
            {"stream_reset": False, "new_records": [], "next_cursor": cursor},
        )()


@pytest.mark.asyncio
async def test_poll_switches_to_compressing(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen._active_task_id = "t1"
        app.active_runner = _RunningRunner()
        screen._store = _FakeStore()
        screen._cursor = None
        screen._poll_active()
        await pilot.pause()
        inds = pilot.app.query(ThinkingIndicator)
        assert len(inds) == 1
        assert inds[0]._state == "compressing"
