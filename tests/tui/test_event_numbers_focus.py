"""На фокусе строки события [N]-префикс НЕ появляется (убран — дёргал ленту и
мешал выделению; фокус показывается фоном :focus)."""
import json
from pathlib import Path

import pytest


@pytest.mark.asyncio
async def test_event_number_not_shown_on_focus(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.user_reply import UserReplyContainer

    tid = "t1"
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text(
        json.dumps({"type": "message", "seq": 1, "ts_utc": "...",
                    "payload": {"id": "u1", "role": "user", "content": "hi"}}) + "\n",
        encoding="utf-8")
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "updated_at": "...", "messages_count": 1}),
        encoding="utf-8")

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause(0.2)

        reply = pilot.app.query_one(UserReplyContainer)
        # До фокуса префикса нет.
        assert "[1]" not in str(reply.render())

        reply.focus()
        await pilot.pause(0.1)

        assert pilot.app.focused is reply
        # На фокусе [N] больше НЕ появляется (фокус — через :focus background).
        assert "[1]" not in str(reply.render())
