import os

from gc_core.thread_store import delete_thread


def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def test_delete_thread_removes_all_shapes(tmp_path):
    d = str(tmp_path)
    sys_, tid, other = "cli", "abc123", "zzz999"
    # session lock живёт в подпапке locks/
    os.makedirs(os.path.join(d, "locks"), exist_ok=True)
    files_in_root = [
        f"data_{sys_}_{tid}.jsonl",
        f"meta_{sys_}_{tid}.json",  # canonical
        f"data_{tid}.jsonl",
        f"meta_{tid}.json",  # legacy
        f"data_{sys_}_{tid}__pre_summary__20260101_000000.jsonl",  # архив 1
        f"meta_{sys_}_{tid}__pre_summary__20260101_000000.json",
        f"data_{sys_}_{tid}__pre_summary__20260102_000000.jsonl",  # архив 2
        f"meta_{sys_}_{tid}__pre_summary__20260102_000000.json",
    ]
    files_in_locks = [f"lock_{sys_}_{tid}.json"]  # session lock
    for name in files_in_root:
        _touch(os.path.join(d, name))
    for name in files_in_locks:
        _touch(os.path.join(d, "locks", name))
    _touch(os.path.join(d, f"data_{sys_}_{other}.jsonl"))  # соседний тред

    deleted = delete_thread(d, sys_, tid)

    for name in files_in_root:
        assert not os.path.exists(os.path.join(d, name)), name
    for name in files_in_locks:
        assert not os.path.exists(os.path.join(d, "locks", name)), (
            f"locks/{name}"
        )
    assert os.path.exists(os.path.join(d, f"data_{sys_}_{other}.jsonl"))
    all_files = files_in_root + files_in_locks
    assert len(deleted) == len(all_files)


def test_delete_thread_missing_is_noop(tmp_path):
    assert delete_thread(str(tmp_path), "cli", "nope") == []
