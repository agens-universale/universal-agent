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
    создаёт каталог и meta с thread_id/system_id + патч (первая запись TUI
    может случиться раньше первой записи executor'а)."""
    threads_dir = tmp_path / "project" / "threads"
    assert not threads_dir.exists()
    store = FileTaskStore(threads_dir=str(threads_dir), system="cli")
    store.update_tui_meta("t9", {"skill_id": "supercoder"})

    meta_path = threads_dir / "meta_cli_t9.json"
    assert meta_path.exists()
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["thread_id"] == "t9"
    assert meta["system_id"] == "cli"
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
