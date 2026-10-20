"""ListTasks: курсор не съезжает при смене timestamp."""

from gc_server.a2a.listing import page
from gc_server.a2a.params import TaskQuery
from gc_server.a2a.store import Task


def _task(task_id: str, sequence: int, timestamp: str) -> Task:
    return Task(
        id=task_id,
        context_id="ctx",
        state="TASK_STATE_COMPLETED",
        timestamp=timestamp,
        history=[],
        sequence=sequence,
    )


def _query(*, page_size: int = 1, cursor=None) -> TaskQuery:
    return TaskQuery(
        context_id=None,
        status=None,
        after=None,
        history_length=None,
        page_size=page_size,
        cursor=cursor,
        include_artifacts=False,
    )


def _cursor(token: str) -> tuple[str, int]:
    stamp, _, sequence = token.rpartition("|")
    return stamp, int(sequence)


def test_page_token_is_keyset_not_offset():
    rows = [
        _task("a", 1, "2026-10-12T15:04:05.001Z"),
        _task("b", 2, "2026-10-12T15:04:05.002Z"),
        _task("c", 3, "2026-10-12T15:04:05.003Z"),
    ]
    first = page(rows, _query())
    assert first["tasks"][0]["id"] == "c"
    assert first["nextPageToken"] == "2026-10-12T15:04:05.003Z|3"
    assert first["totalSize"] == 3
    second = page(rows, _query(cursor=_cursor(first["nextPageToken"])))
    assert second["tasks"][0]["id"] == "b"
    assert second["totalSize"] == 3


def test_page_cursor_does_not_repeat_when_timestamp_moves():
    rows = [
        _task("a", 1, "2026-10-12T15:04:05.001Z"),
        _task("b", 2, "2026-10-12T15:04:05.002Z"),
        _task("c", 3, "2026-10-12T15:04:05.003Z"),
    ]
    first = page(rows, _query())
    assert first["tasks"][0]["id"] == "c"
    rows[1] = _task("b", 2, "2026-10-12T15:04:05.009Z")
    second = page(rows, _query(cursor=_cursor(first["nextPageToken"])))
    assert second["tasks"][0]["id"] != "c"
    assert second["tasks"][0]["id"] == "a"
