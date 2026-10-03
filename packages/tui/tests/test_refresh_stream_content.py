from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {
        "thread_id": tid,
        "status": "running",
        "updated_at": "2026-06-15T12:00:00+00:00",
        "messages_count": 0,
    }
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


class _FakeRunner:
    def is_running(self) -> bool:
        return True

    def stop(self) -> None:
        pass


@pytest.mark.asyncio
async def test_refresh_updates_mounted_answer_row(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    tid = "ref1"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "model": "M",
                "backend_api_mode": "chat_completions",
                "tui_stream_display": "answer",
            }
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        pilot.app._stream_sink.push("answer", "первая часть")
        await pilot.pause(0.7)  # poll смонтировал answer-row
        assert scr._stream_answer_row is not None
        pilot.app._stream_sink.push("answer", " и вторая")
        scr._refresh_stream_content()  # прямой вызов (как событие)
        content = scr._stream_answer_row.query_one(".trace-content", Static)
        assert "и вторая" in str(content.render())


@pytest.mark.asyncio
async def test_refresh_noop_when_nothing_mounted(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "ref2"
    _write_task(tmp_path, tid)
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {"model": "M", "backend_api_mode": "chat_completions"}
        ),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        scr._refresh_stream_content()  # ни виджета — без исключений
        assert scr._stream_answer_row is None
