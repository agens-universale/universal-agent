"""Исход сессии в списке /sessions: meta.tui.status > верхний meta.status > None.
Регресс-страховка: раньше list_tasks дефолтил в «stopped» и игнорировал tui.status,
из-за чего ВСЕ сессии показывались [stopped]."""
import json
from pathlib import Path

from gc_tui.services.stores.file_store import FileTaskStore


def _write_meta(d: Path, system: str, tid: str, meta: dict) -> None:
    (d / f"meta_{system}_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def test_list_status_prefers_tui_then_top_then_none(tmp_path):
    sysid = "cli"
    _write_meta(tmp_path, sysid, "a", {"thread_id": "a", "tui": {"status": "completed"}})
    _write_meta(tmp_path, sysid, "b", {"thread_id": "b", "status": "running"})        # только верхний
    _write_meta(tmp_path, sysid, "c", {"thread_id": "c"})                              # исход не записан
    _write_meta(tmp_path, sysid, "d", {"thread_id": "d", "status": "running",
                                       "tui": {"status": "stopped"}})                  # tui приоритетнее

    store = FileTaskStore(threads_dir=str(tmp_path), system=sysid)
    by_id = {t.thread_id: t.status for t in store.list_tasks()}

    assert by_id == {"a": "completed", "b": "running", "c": None, "d": "stopped"}
