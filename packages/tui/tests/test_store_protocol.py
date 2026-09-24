"""TaskStore Protocol — структурный контракт (без реализации)."""

from gc_tui.services.stores.base import TaskStore


def test_protocol_exposes_four_methods():
    expected = {
        "list_tasks",
        "get_task_meta",
        "read_all_events",
        "tail_events",
    }
    have = {m for m in dir(TaskStore) if not m.startswith("_")}
    assert expected.issubset(have)
