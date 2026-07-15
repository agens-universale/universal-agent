import os

from thread_store import delete_thread


def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def test_delete_thread_removes_all_shapes(tmp_path):
    d = str(tmp_path)
    sys_, tid, other = "cli", "abc123", "zzz999"
    files = [
        f"data_{sys_}_{tid}.jsonl", f"meta_{sys_}_{tid}.json",          # canonical
        f"lock_{sys_}_{tid}.json",                                      # session lock
        f"data_{tid}.jsonl", f"meta_{tid}.json",                        # legacy
        f"data_{sys_}_{tid}__pre_summary__20260101_000000.jsonl",       # архив 1
        f"meta_{sys_}_{tid}__pre_summary__20260101_000000.json",
        f"data_{sys_}_{tid}__pre_summary__20260102_000000.jsonl",       # архив 2
        f"meta_{sys_}_{tid}__pre_summary__20260102_000000.json",
    ]
    for name in files:
        _touch(os.path.join(d, name))
    _touch(os.path.join(d, f"data_{sys_}_{other}.jsonl"))  # соседний тред

    deleted = delete_thread(d, sys_, tid)

    for name in files:
        assert not os.path.exists(os.path.join(d, name)), name
    assert os.path.exists(os.path.join(d, f"data_{sys_}_{other}.jsonl"))
    assert len(deleted) == len(files)


def test_delete_thread_missing_is_noop(tmp_path):
    assert delete_thread(str(tmp_path), "cli", "nope") == []
