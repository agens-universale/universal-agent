"""Суммаризация не трогает touched journal (только delete/reset треда)."""

from __future__ import annotations

import json
from pathlib import Path

from gc_core.thread_store import (
    ThreadSession,
    delete_thread,
    touched_journal_path,
)
from gc_testkit.settings import make_settings
from gc_tui.diff_tui.touched import TouchedJournal


async def test_summary_leaves_touched_journal_intact(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "arch1"
    j = TouchedJournal(str(threads), "tui", tid)
    j.add("src/a.py")
    touched = Path(touched_journal_path(str(threads), "tui", tid))
    assert touched.is_file()
    before = touched.read_text(encoding="utf-8")

    data = threads / f"data_tui_{tid}.jsonl"
    meta = threads / f"meta_tui_{tid}.json"
    data.write_text(
        json.dumps(
            {
                "type": "message",
                "thread_id": tid,
                "system": "tui",
                "seq": 1,
                "ts": "2026-01-01T00:00:00+00:00",
                "payload": {"role": "user", "content": "hi", "id": "m1"},
            }
        )
        + "\n",
        encoding="utf-8",
    )
    meta.write_text(
        json.dumps({"thread_id": tid, "system": "tui", "messages_count": 1}),
        encoding="utf-8",
    )

    session = await ThreadSession.afrom_config(
        settings=make_settings({"threads_data_path": str(threads)}),
        system_id="tui",
        thread_id=tid,
    )
    result = await session.aarchive_and_compact_summary_payload(
        {
            "summary_message_content": "summary",
            "dialog_steps_offset": 0,
        }
    )
    assert "archive_touched_path" not in result
    assert touched.is_file()
    assert touched.read_text(encoding="utf-8") == before
    assert TouchedJournal(str(threads), "tui", tid).paths() == {"src/a.py"}
    assert not list(threads.glob(f"touched_tui_{tid}__pre_summary__*.json"))


def test_delete_thread_removes_touched_and_legacy_archives(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "del1"
    j = TouchedJournal(str(threads), "tui", tid)
    j.add("src/a.py")
    touched = Path(touched_journal_path(str(threads), "tui", tid))
    legacy = threads / f"touched_tui_{tid}__pre_summary__20260101_000000.json"
    legacy.write_text('{"paths": {}}\n', encoding="utf-8")
    (threads / f"data_tui_{tid}.jsonl").write_text("", encoding="utf-8")
    (threads / f"meta_tui_{tid}.json").write_text("{}", encoding="utf-8")

    deleted = delete_thread(str(threads), "tui", tid)
    assert str(touched) in deleted
    assert str(legacy) in deleted
    assert not touched.exists()
    assert not legacy.exists()


def test_legacy_gc_touched_json_ignored(tmp_path, monkeypatch):
    """Старый ``.gc/diffs/touched.json`` не читаем."""
    from gc_core.config_modules import env_vars
    from gc_tui.diff_tui.service import DiffService

    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    root = tmp_path / "proj"
    root.mkdir()
    threads = tmp_path / "threads"
    threads.mkdir()
    gc = root / ".gc" / "diffs"
    gc.mkdir(parents=True)
    (gc / "touched.json").write_text(
        json.dumps({"paths": {"ghost.py": {"ts": 1}}}), encoding="utf-8"
    )
    (root / "ghost.py").write_text("x\n", encoding="utf-8")
    svc = DiffService(
        str(root),
        threads_dir=str(threads),
        system_id="tui",
        thread_id="t1",
        emit_notices=False,
    )
    assert svc.list_entries(pending_only=True) == []
