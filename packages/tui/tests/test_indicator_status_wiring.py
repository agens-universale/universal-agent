import json

import pytest


@pytest.mark.asyncio
async def test_instant_stop_leaves_no_indicator(tmp_path, monkeypatch):
    """Стоп хода, который тут же кончился: исхода в индикаторе нет (S10
    §3.3) — индикатор и бар уходят в том же вызове, строка итога — в
    ленте (`test_stopping_indicator`)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    threads = tmp_path / "threads"
    threads.mkdir()
    async with TuiApp(threads_dir=str(threads)).run_test() as pilot:
        scr = pilot.app.screen

        # Живой runner — ДО индикатора: иначе тик опроса на паузе видит
        # простой без задачи и убирает индикатор (флап при запуске файла).
        class _R:
            running = True

            def is_running(self):
                return self.running

            def stop(self):
                self.running = False  # ход кончился сразу: итог ровный

        # Ход открытого треда: строка итога встаёт в его ленте.
        tid = "stop0000000000000000000000000001"
        (threads / f"meta_cli_{tid}.json").write_text(
            json.dumps({"thread_id": tid}), encoding="utf-8"
        )
        (threads / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
        scr.attach_active_task(tid, is_resume=True)
        scr.app.active_runner = _R()
        scr._prepare_for_new_run()
        # смонтировать индикатор как при живом прогоне
        scr._thinking_started_at = 0.0
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()

        scr._cmd_stop()
        await pilot.pause()
        assert scr._thinking is None
        assert not scr.query_one("#indicator-bar").has_class("-turn")
        from gc_tui.widgets.turn_line import TurnLine

        (line,) = scr.query_one("#trace").query(TurnLine)
        assert line.text.plain == "■ Отменено"
        assert line.display


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

            def get_task_meta(self, tid):
                return {}

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
        # Исход — статусом (кэш списка сессий); вид, время и причина хода —
        # в записи итога ядра, в мете их нет (решение плана 5).
        stamped = [p for p in patches if "last_indicator" in p]
        assert stamped == [
            {"status": "stopped", "last_indicator": {"edits": None}}
        ], patches
