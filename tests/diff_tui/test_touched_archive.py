"""Archive rename для touched journal (spec step 6)."""
from __future__ import annotations

import json
from pathlib import Path

from diff_tui.touched import TouchedJournal
from thread_store import ThreadSession, touched_journal_path


def test_archive_renames_touched_with_same_suffix(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "arch1"
    j = TouchedJournal(str(threads), "tui", tid)
    j.add("src/a.py")
    touched = Path(touched_journal_path(str(threads), "tui", tid))
    assert touched.is_file()

    # минимальная live session
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

    session = ThreadSession.from_config(
        config_dict={"threads_data_path": str(threads)},
        system_id="tui",
        thread_id=tid,
    )
    result = session.archive_and_compact_summary_payload(
        {
            "summary_message_content": "summary",
            "dialog_steps_offset": 0,
        }
    )
    assert result["archive_touched_path"] is not None
    assert Path(result["archive_touched_path"]).is_file()
    assert not touched.exists()
    # live journal убран; после archive новый пустой не создаём автоматически
    assert TouchedJournal(str(threads), "tui", tid).paths() == set()


def test_legacy_gc_touched_json_ignored(tmp_path, monkeypatch):
    """Старый ``.gc/diffs/touched.json`` не читаем."""
    from config_modules import env_vars
    from diff_tui.service import DiffService

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
