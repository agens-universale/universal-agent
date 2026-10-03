"""'/new' создаёт новую задачу в текущем окне. При активном running — y/n confirm."""

from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_new_when_idle_goes_to_empty_state(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "existing"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts_utc": "2026-05-21T12:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "x"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "status": "stopped",
                "updated_at": "2026-05-21T12:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)
        assert app.tui_state.active_task_id == tid

        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/new"
        await pilot.press("enter")
        await pilot.pause(0.2)

        assert app.tui_state.active_task_id is None
        assert pilot.app.screen.query("#brand-block")


@pytest.mark.asyncio
async def test_new_when_running_asks_confirm(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    stop_called = {"flag": False}

    class _Fake:
        def __init__(self):
            self._evt = None

        async def arun(self, messages, *, thread_id, **kw):
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            stop_called["flag"] = True
            if self._evt:
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app._executor_factory = lambda: _Fake()

    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.2)

        inp.text = "/new"
        await pilot.press("enter")
        await pilot.pause(0.2)
        assert any(
            isinstance(s, ConfirmScreen) for s in pilot.app.screen_stack
        )

        await pilot.press("y")
        await pilot.pause(0.3)
        assert stop_called["flag"] is True
        assert app.tui_state.active_task_id is None


@pytest.mark.asyncio
async def test_new_confirm_has_title_and_wording(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.confirm import ConfirmScreen

    class _Fake:
        def __init__(self):
            self._evt = None

        async def arun(self, messages, *, thread_id, **kw):
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if self._evt:
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.2)
        inp.text = "/new"
        await pilot.press("enter")
        await pilot.pause(0.2)
        modal = next(
            s for s in pilot.app.screen_stack if isinstance(s, ConfirmScreen)
        )
        assert (
            modal.query_one("#confirm-dialog").border_title == "Новая задача"
        )
        hint = str(modal.query_one("#confirm-hint").render())
        assert "прервать" in hint and "продолжить" in hint


@pytest.mark.asyncio
async def test_new_confirm_yes_clears_runner_and_indicator(tmp_path):
    """Regression: после /new+yes индикатор «думаю» не висит, runner отвязан."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    class _Fake:
        def __init__(self):
            self._evt = None

        async def arun(self, messages, *, thread_id, **kw):
            import threading

            self._evt = threading.Event()
            self._evt.wait(timeout=3.0)

        def stop_run(self):
            if self._evt:
                self._evt.set()

        def add_message(self, m):
            pass

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    app._executor_factory = lambda: _Fake()
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "стартуй"
        await pilot.press("enter")
        await pilot.pause(0.3)
        inp.text = "/new"
        await pilot.press("enter")
        await pilot.pause(0.2)
        await pilot.press("y")
        await pilot.pause(0.5)  # ≥1 poll-тик (250мс)
        assert app.active_runner is None
        assert len(pilot.app.screen.query(ThinkingIndicator)) == 0
        assert pilot.app.screen.query("#brand-block")
