"""Список сессий заполняет высоту экрана (#sessions-root растягивается)."""
import json
from pathlib import Path

import pytest

from thread_store import append_new_thread_messages


def _seed(threads_dir, tid, prompt):
    append_new_thread_messages(str(threads_dir), "cli", tid,
                               [{"role": "user", "content": prompt, "id": f"m_{tid}"}], set())
    (Path(threads_dir) / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system": "cli", "status": "stopped",
                    "updated_at": "2026-05-30T10:00:00+00:00", "messages_count": 1}),
        encoding="utf-8")


@pytest.mark.asyncio
async def test_sessions_list_fills_height(tmp_path):
    pytest.importorskip("textual")
    for i in range(3):
        _seed(tmp_path, f"fs{i:04d}", f"промпт {i}")
    from tui.screens.sessions import SessionsScreen
    from tui.app import TuiApp
    async with TuiApp(threads_dir=str(tmp_path), system="cli").run_test(size=(100, 40)) as pilot:
        await pilot.app.push_screen(SessionsScreen(threads_dir=str(tmp_path), system="cli"))
        await pilot.pause()
        from textual.containers import VerticalScroll
        lst = pilot.app.screen.query_one("#sessions-list", VerticalScroll)
        # На терминале высотой 40 с только 3 короткими карточками список ДОЛЖЕН
        # растянуться на всю свободную высоту (≈ 40 минус шапка/фильтр/футер),
        # а не сжаться до высоты 3 карточек. Порог 30 надёжно различает.
        assert lst.size.height >= 30, f"список не на всю высоту: {lst.size.height}"
