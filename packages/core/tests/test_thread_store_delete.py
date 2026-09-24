import os

from gc_core.thread_store import delete_thread, edits_journal_path


def _touch(path: str) -> None:
    with open(path, "w", encoding="utf-8") as f:
        f.write("x")


def test_delete_thread_removes_all_shapes(tmp_path):
    d = str(tmp_path)
    sys_, tid, other = "cli", "abc123", "zzz999"
    # session lock живёт в подпапке locks/
    os.makedirs(os.path.join(d, "locks"), exist_ok=True)
    files_in_root = [
        f"data_{sys_}_{tid}.jsonl", f"meta_{sys_}_{tid}.json",          # canonical
        f"data_{tid}.jsonl", f"meta_{tid}.json",                        # legacy
        f"data_{sys_}_{tid}__pre_summary__20260101_000000.jsonl",       # архив 1
        f"meta_{sys_}_{tid}__pre_summary__20260101_000000.json",
        f"data_{sys_}_{tid}__pre_summary__20260102_000000.jsonl",       # архив 2
        f"meta_{sys_}_{tid}__pre_summary__20260102_000000.json",
    ]
    files_in_locks = [f"lock_{sys_}_{tid}.json"]                        # session lock
    for name in files_in_root:
        _touch(os.path.join(d, name))
    for name in files_in_locks:
        _touch(os.path.join(d, "locks", name))
    _touch(os.path.join(d, f"data_{sys_}_{other}.jsonl"))  # соседний тред

    deleted = delete_thread(d, sys_, tid)

    for name in files_in_root:
        assert not os.path.exists(os.path.join(d, name)), name
    for name in files_in_locks:
        assert not os.path.exists(os.path.join(d, "locks", name)), f"locks/{name}"
    assert os.path.exists(os.path.join(d, f"data_{sys_}_{other}.jsonl"))
    all_files = files_in_root + files_in_locks
    assert len(deleted) == len(all_files)


def test_delete_thread_removes_edits_journal_lock(tmp_path):
    """См. `test_reset_thread_files_deletes_edits_journal_lock`: тот же лок
    журнала правок — сирота и после `delete_thread`, не только после сброса."""
    d = str(tmp_path)
    journal = edits_journal_path(d, "cli", "abc123")
    journal.write_text("", encoding="utf-8")
    journal_lock_path = str(journal) + ".lock"
    _touch(journal_lock_path)

    deleted = delete_thread(d, "cli", "abc123")

    assert str(journal) in deleted
    assert journal_lock_path in deleted
    assert not os.path.exists(journal_lock_path)


def test_delete_thread_missing_is_noop(tmp_path):
    assert delete_thread(str(tmp_path), "cli", "nope") == []


def test_delete_thread_keeps_going_when_one_file_refuses_to_die(tmp_path, monkeypatch):
    """Та же причина, что у сброса: лок журнала чужой процесс держит открытым,
    и на Windows `os.remove` по нему даёт `PermissionError`. Голый цикл ронял
    удаление треда посреди работы, оставляя половину файлов."""
    d = str(tmp_path)
    journal = str(edits_journal_path(d, "cli", "abc123"))
    journal_lock = journal + ".lock"
    data = os.path.join(d, "data_cli_abc123.jsonl")
    _touch(journal)
    _touch(journal_lock)
    _touch(data)

    real_remove = os.remove

    def stubborn_remove(path, *args, **kwargs):
        if str(path) == journal_lock:
            raise PermissionError(22, "файл занят другим процессом")
        return real_remove(path, *args, **kwargs)

    monkeypatch.setattr(os, "remove", stubborn_remove)

    deleted = delete_thread(d, "cli", "abc123")

    assert data in deleted
    assert journal in deleted
    assert journal_lock not in deleted
    assert os.path.exists(journal_lock)
