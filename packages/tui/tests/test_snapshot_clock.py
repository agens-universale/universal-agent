"""Часы эталонов TUI закреплены в одном месте (`gc_testkit.tui_support`):
«сейчас» и местный пояс не зависят от даты и пояса машины, поэтому время
строки итога в кадре детерминировано."""

import os
import time
from datetime import UTC, datetime


def test_pinned_snapshot_clock_fixes_now_and_zone(monkeypatch):
    from gc_testkit.tui_support import SNAPSHOT_NOW, pinned_snapshot_clock
    from gc_tui.services import relative_time

    monkeypatch.setenv("TZ", "America/Los_Angeles")
    time.tzset()
    ended = datetime(2026, 10, 22, 11, 32, 12, tzinfo=UTC)
    with pinned_snapshot_clock(monkeypatch):
        assert relative_time._now_local() == SNAPSHOT_NOW
        assert ended.astimezone().strftime("%H:%M") == "13:32"
        assert relative_time.format_turn_clock(ended.isoformat()) == "13:32"
    assert os.environ["TZ"] == "America/Los_Angeles"
    assert ended.astimezone().strftime("%H:%M") == "04:32"
    monkeypatch.undo()
    time.tzset()
