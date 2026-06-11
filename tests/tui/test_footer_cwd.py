"""Footer-right на главном экране показывает рабочую папку сессии (Path.cwd())."""
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_footer_shows_session_cwd(tmp_path):
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    tid = "t1"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "status": "stopped",
                    "updated_at": "2026-05-22T12:00:00+00:00", "messages_count": 1}),
        encoding="utf-8")
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "2026-05-22T12:00:00+00:00",
                    "payload": {"id": "u1", "role": "user", "content": "x"}}) + "\n",
        encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause(0.2)
        right = str(pilot.app.screen.query_one("#footer-right", Static).render())
        # Хвост текущей рабочей папки процесса (чип усекает слева длинный путь).
        assert Path.cwd().name in right
