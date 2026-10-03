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


@pytest.mark.asyncio
async def test_stream_signal_delegates_to_active_screen(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    tid = "sig1"
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
        calls = []
        monkeypatch.setattr(
            scr, "_refresh_stream_content", lambda: calls.append(1)
        )
        pilot.app._stream_signal()
        assert calls == [1]
