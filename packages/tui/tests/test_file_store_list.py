"""FileTaskStore.list_tasks + get_task_meta — поверх существующего thread_store."""

import json
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore


def _write_thread(
    threads_dir: Path,
    system: str,
    tid: str,
    *,
    msgs: int,
    updated: str,
    skill: str | None = None,
    error: str | None = None,
    status: str = "stopped",
):
    """Создать пару data_*.jsonl + meta_*.json в формате thread_store."""
    data = threads_dir / f"data_{system}_{tid}.jsonl"
    meta = threads_dir / f"meta_{system}_{tid}.json"
    data.write_text(
        "\n".join(
            json.dumps({"type": "message", "seq": i, "payload": {"id": f"m{i}", "role": "user", "content": "x"}})
            for i in range(msgs)
        )
    )
    meta_obj = {
        "thread_id": tid,
        "system_id": system,
        "updated_at": updated,
        "messages_count": msgs,
    }
    if skill is not None:
        meta_obj.setdefault("tui", {})["skill_id"] = skill
    if error is not None:
        meta_obj["last_error"] = error
    meta.write_text(json.dumps(meta_obj))


def test_list_tasks_scans_meta_and_returns_summary(tmp_path):
    _write_thread(tmp_path, "cli", "t1", msgs=3, updated="2026-05-20T10:00:00+00:00", skill="swe-task-solver")
    _write_thread(tmp_path, "cli", "t2", msgs=1, updated="2026-05-20T09:00:00+00:00", error="ENOENT")

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    tasks = store.list_tasks()

    ids = {t.thread_id for t in tasks}
    assert ids == {"t1", "t2"}

    by_id = {t.thread_id: t for t in tasks}
    assert by_id["t1"].messages_count == 3
    assert by_id["t1"].skill_id == "swe-task-solver"
    assert by_id["t2"].last_error == "ENOENT"


def test_list_tasks_sorted_by_updated_desc(tmp_path):
    _write_thread(tmp_path, "cli", "older", msgs=1, updated="2026-05-19T10:00:00+00:00")
    _write_thread(tmp_path, "cli", "newer", msgs=1, updated="2026-05-20T10:00:00+00:00")

    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    tasks = store.list_tasks()
    assert [t.thread_id for t in tasks] == ["newer", "older"]


def test_get_task_meta_returns_full_dict(tmp_path):
    _write_thread(tmp_path, "cli", "t1", msgs=2, updated="2026-05-20T10:00:00+00:00")
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    meta = store.get_task_meta("t1")
    assert meta["thread_id"] == "t1"
    assert meta["messages_count"] == 2


def test_list_tasks_offset_limit(tmp_path):
    for i in range(5):
        _write_thread(tmp_path, "cli", f"t{i}", msgs=1, updated=f"2026-05-2{i}T10:00:00+00:00")
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    tasks = store.list_tasks(limit=2, offset=1)
    assert len(tasks) == 2


def _write_meta(
    threads_dir: Path,
    thread_id: str,
    *,
    system: str = "cli",
    suffix: str = "",
    updated_at: str = "2026-05-29T10:00:00+00:00",
) -> None:
    name = f"meta_{system}_{thread_id}{suffix}.json"
    (threads_dir / name).write_text(
        json.dumps(
            {
                "thread_id": thread_id,
                "status": "stopped",
                "updated_at": updated_at,
                "messages_count": 0,
                "tui": {},
            }
        ),
        encoding="utf-8",
    )


def test_list_tasks_skips_pre_summary_snapshots(tmp_path):
    """Snapshot-файлы (meta_<sys>_<id>__pre_summary__<ts>.json) НЕ попадают
    в list_tasks как отдельные треды — это дубликаты live-meta."""
    threads = tmp_path / "threads"
    threads.mkdir()
    # 1 live + 2 snapshot-а одного и того же треда.
    _write_meta(threads, "thread1", updated_at="2026-05-29T10:00:00+00:00")
    _write_meta(threads, "thread1", suffix="__pre_summary__20260529_090000", updated_at="2026-05-29T09:00:00+00:00")
    _write_meta(threads, "thread1", suffix="__pre_summary__20260529_080000", updated_at="2026-05-29T08:00:00+00:00")
    # И ещё один независимый тред.
    _write_meta(threads, "thread2", updated_at="2026-05-29T09:30:00+00:00")
    store = FileTaskStore(threads_dir=str(threads), system="cli")
    tasks = store.list_tasks()
    ids = [t.thread_id for t in tasks]
    assert ids == ["thread1", "thread2"], (
        f"snapshot должны быть отфильтрованы, в /sessions только live-треды, got: {ids}"
    )


def test_list_tasks_returns_live_threads_without_snapshots(tmp_path):
    """Регрессия: без snapshot-файлов list_tasks работает как раньше."""
    threads = tmp_path / "threads"
    threads.mkdir()
    _write_meta(threads, "alpha", updated_at="2026-05-29T10:00:00+00:00")
    _write_meta(threads, "beta", updated_at="2026-05-29T09:00:00+00:00")
    store = FileTaskStore(threads_dir=str(threads), system="cli")
    tasks = store.list_tasks()
    assert [t.thread_id for t in tasks] == ["alpha", "beta"]
