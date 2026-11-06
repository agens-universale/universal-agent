from gc_testkit.develop_files import record_from_meta
import pytest


@pytest.mark.asyncio
async def test_cancel_sets_indicator_cancelled(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen

        # Живой runner — ДО индикатора: иначе тик опроса на паузе видит
        # простой без задачи и убирает индикатор (флап при запуске файла).
        class _R:
            running = True

            def is_running(self):
                return self.running

            def stop(self):
                self.running = False  # ход кончился сразу: итог ровный

        scr.app.active_runner = _R()
        scr.app.tui_state.active_task_id = "t1"
        scr._active_task_id = "t1"
        scr._events_since_last_user_send = 3  # не cancel-with-undo
        # смонтировать индикатор как при живом прогоне
        scr._thinking_started_at = 0.0
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()

        scr._cmd_stop()
        await pilot.pause()
        assert scr._thinking is not None
        assert scr._thinking._state == "cancelled"
        assert scr._thinking._render_text().plain == "■ Отменено"


@pytest.mark.asyncio
async def test_falling_edge_persists_last_indicator(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        # стор-двойник: фиксируем patch'и update_tui_meta
        patches = []

        class _Store:
            def update_tui_meta(self, tid, patch):
                patches.append(patch)

            def get_record(self, tid):
                return record_from_meta({})

        scr._store = _Store()
        scr._thinking_started_at = 0.0
        scr._was_running = True
        monkeypatch.setattr(
            type(scr), "_update_terminal_title", lambda self: None
        )

        class _R:
            def is_running(self):
                return False

            _thread_kind = "run"

        scr.app.active_runner = _R()
        scr.app.last_runner_error = None
        scr.app.last_stop_requested = True  # => stopped → cancelled

        scr._poll_active()
        kinds = [
            p.get("last_indicator", {}).get("kind")
            for p in patches
            if "last_indicator" in p
        ]
        assert kinds == ["cancelled"], patches
