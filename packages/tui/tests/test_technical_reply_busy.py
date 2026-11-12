"""Техническая реплика мимо своего хода (отмена правки, вложение) в тред,
который сейчас ведёт другой процесс: предупреждение, а не падение TUI."""

from __future__ import annotations

import pytest
from gc_core.threads.errors import ThreadBusy
from gc_tui.screens.main import THREAD_BUSY_REPLY
from gc_tui.services.stores.file_store import FileTaskStore

_TID = "busyreply000000000000000000000001"


@pytest.mark.asyncio
async def test_busy_thread_warns_instead_of_crashing(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    def busy(self, task_id, messages, system=None):
        raise ThreadBusy("тред занят", owner="gc-cli")

    monkeypatch.setattr(FileTaskStore, "append_messages", busy)
    notifications = []
    async with TuiApp(
        threads_dir=str(tmp_path), system="cli"
    ).run_test() as pilot:
        pilot.app.notify = lambda msg, **kw: notifications.append(
            (msg, kw.get("severity"))
        )
        scr = pilot.app.screen
        scr._active_task_id = _TID
        scr._append_technical_to_thread(
            {"role": "user", "content": "правка отменена"}
        )
    assert (THREAD_BUSY_REPLY, "warning") in notifications
