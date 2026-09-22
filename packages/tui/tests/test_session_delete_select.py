from datetime import UTC, datetime

from gc_tui.models import TaskSummary
from gc_tui.services.session_delete_select import select_for_deletion


def _task(tid: str, updated_at: str) -> TaskSummary:
    return TaskSummary(thread_id=tid, status="stopped", updated_at=updated_at,
                       messages_count=1, skill_id=None, unread_count=0, last_error=None)


def _tasks():
    # 5 сессий: t00 самая свежая (06-05) ... t04 самая старая (06-01)
    return [_task(f"t{i:02d}", f"2026-06-{5 - i:02d}T00:00:00+00:00") for i in range(5)]


def test_keep_last_drops_oldest():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    ids = select_for_deletion(_tasks(), keep_last=2, now=now)
    assert ids == ["t02", "t03", "t04"]


def test_keep_last_ge_total_deletes_nothing():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    assert select_for_deletion(_tasks(), keep_last=10, now=now) == []


def test_keep_last_zero_deletes_all():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    assert len(select_for_deletion(_tasks(), keep_last=0, now=now)) == 5


def test_older_than_days_cutoff():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    ids = select_for_deletion(_tasks(), older_than_days=3, now=now)
    assert set(ids) == {"t03", "t04"}


def test_older_than_unparsable_kept():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    tasks = [_task("good", "2026-01-01T00:00:00+00:00"), _task("bad", "не-дата")]
    ids = select_for_deletion(tasks, older_than_days=1, now=now)
    assert ids == ["good"]


def test_empty_input():
    now = datetime(2026, 6, 6, tzinfo=UTC)
    assert select_for_deletion([], keep_last=5, now=now) == []
