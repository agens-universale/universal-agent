"""FileTaskStore.update_tui_meta — обновление meta.tui sub-dict."""

import json
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore


def test_update_tui_meta_creates_skills_list(tmp_path):
    tid = "t1"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {
                "thread_id": tid,
                "updated_at": "2026-05-21T12:00:00+00:00",
                "messages_count": 1,
            }
        ),
        encoding="utf-8",
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    store.update_tui_meta(tid, {"skills": ["supercoder"]})

    meta = json.loads(
        (Path(tmp_path) / f"meta_cli_{tid}.json").read_text(encoding="utf-8")
    )
    assert meta["tui"]["skills"] == ["supercoder"]


def test_update_tui_meta_creates_meta_and_dir_when_missing(tmp_path):
    """Свежий проект: ни каталога threads, ни meta-файла нет — update_tui_meta
    создаёт каталог и meta с thread_id/system + патч (первая запись TUI
    может случиться раньше первой записи executor'а)."""
    threads_dir = tmp_path / "project" / "threads"
    assert not threads_dir.exists()
    store = FileTaskStore(threads_dir=str(threads_dir), system="cli")
    store.update_tui_meta("t9", {"skill_id": "supercoder"})

    meta_path = threads_dir / "meta_cli_t9.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["thread_id"] == "t9"
    assert meta["system"] == "cli"
    assert meta["tui"]["skill_id"] == "supercoder"


def test_update_tui_meta_merges_existing(tmp_path):
    tid = "t2"
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(
            {"thread_id": tid, "tui": {"skills": ["a"], "cwd": "/old"}}
        ),
        encoding="utf-8",
    )
    store = FileTaskStore(threads_dir=str(tmp_path), system="cli")
    store.update_tui_meta(tid, {"skills": ["a", "b"]})

    meta = json.loads(
        (Path(tmp_path) / f"meta_cli_{tid}.json").read_text(encoding="utf-8")
    )
    assert meta["tui"]["skills"] == ["a", "b"]
    assert meta["tui"]["cwd"] == "/old"


def test_update_tui_meta_races_executor_without_broken_or_lost_meta(
    tmp_path,
):
    """Мета TUI и исполнителя одного треда пишутся параллельно: исполнитель
    не читает обрезанный файл, ключи исполнителя не теряются."""
    import threading

    from gc_core.threads.facade import run_sync
    from gc_core.threads.file_store import FileThreadStore
    from gc_core.threads.models import RuntimeStatus, SideChange, ThreadKey
    from gc_testkit.develop_files import load_thread_meta

    key = ThreadKey(system="cli", thread="t3")
    runtime_store = FileThreadStore(str(tmp_path))

    def update_runtime(i):
        change = SideChange(
            op_id=f"rt{i}", set_runtime=RuntimeStatus(live_msg_count=i)
        )
        run_sync(runtime_store.append_side(key, change))

    d, tid = str(tmp_path), "t3"
    store = FileTaskStore(threads_dir=d, system="cli")
    store.update_tui_meta(tid, {"status": "running"})
    errors = []
    stop = threading.Event()

    def tui_writer():
        i = 0
        while not stop.is_set():
            store.update_tui_meta(tid, {"copy_buffer": "x" * 2000 + str(i)})
            i += 1

    def executor():
        i = 0
        while not stop.is_set():
            try:
                update_runtime(i)
                load_thread_meta(d, "cli", tid)
            except Exception as exc:
                errors.append(type(exc).__name__)
            i += 1

    threads = [threading.Thread(target=f) for f in (tui_writer, executor)]
    for t in threads:
        t.start()
    stop.wait(1.0)
    stop.set()
    for t in threads:
        t.join()
    meta = load_thread_meta(d, "cli", tid)

    assert sorted(set(errors)) == []
    assert "live_msg_count" in meta["runtime"]
    assert meta["tui"]["status"] == "running"
