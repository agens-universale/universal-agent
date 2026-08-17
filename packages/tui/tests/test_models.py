"""UI-модели (без textual)."""
from gc_tui.models import TaskCursor, TailResult


def test_tail_result_default_reset_false():
    cur = TaskCursor(position_token=0, ordering_key=0, storage_epoch=None)
    r = TailResult(new_records=[], next_cursor=cur)
    assert r.stream_reset is False
    assert r.reason is None
