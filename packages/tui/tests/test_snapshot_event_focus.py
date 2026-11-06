from gc_testkit.tui_support import tui_settings
import json
from pathlib import Path

import pytest

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")


def test_event_focus_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.widgets.user_reply import UserReplyContainer

    # Папку в шапке фиксируем — иначе снапшот стал бы машинно-зависимым.
    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )

    tid = "t1"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps(
            {
                "type": "message",
                "seq": 1,
                "ts_utc": "...",
                "payload": {"id": "u1", "role": "user", "content": "tab меня"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "...",
                "messages_count": 1,
                "tui": {"cwd": "/work/demo"},
            }
        ),
        encoding="utf-8",
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
        await pilot.pause(0.2)
        pilot.app.query_one(UserReplyContainer).focus()
        await pilot.pause(0.1)

    assert snap_compare(app, terminal_size=(120, 24), run_before=setup)
