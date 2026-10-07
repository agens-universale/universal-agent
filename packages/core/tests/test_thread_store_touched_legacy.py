"""Legacy touched-журнал: путь, чистка при удалении треда, суммаризация.

Класс `TouchedJournal` умер вместе с пакетом `diff_tui` — файлы `touched_*.json`
у пользователей остались. `thread_store` обязан их знать: чтобы прибирать за
собой при удалении/сбросе треда и чтобы суммаризация их не трогала.
"""

from __future__ import annotations

import json
from pathlib import Path

from gc_core.thread_store import (
    ThreadSession,
    delete_thread,
    touched_journal_path,
)


def _seed_touched(threads: Path, system_id: str, thread_id: str) -> Path:
    """Legacy-файл в формате умершего журнала — как он лежит у пользователей."""
    path = Path(touched_journal_path(str(threads), system_id, thread_id))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps({"paths": {"src/a.py": {"ts": 1}}}) + "\n", encoding="utf-8"
    )
    return path


def test_path_helper_matches_data_meta_suffix(tmp_path):
    threads = str(tmp_path / "threads")
    path = touched_journal_path(threads, "Tui", "abc-1")
    assert path == str(Path(threads) / "touched_tui_abc-1.json")


async def test_summary_leaves_touched_journal_intact(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "arch1"
    touched = _seed_touched(threads, "tui", tid)
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
        config_dict={"threads_data_path": str(threads)},
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
    assert not list(threads.glob(f"touched_tui_{tid}__pre_summary__*.json"))


def test_delete_thread_removes_touched_and_legacy_archives(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "del1"
    touched = _seed_touched(threads, "tui", tid)
    legacy = threads / f"touched_tui_{tid}__pre_summary__20260101_000000.json"
    legacy.write_text('{"paths": {}}\n', encoding="utf-8")
    (threads / f"data_tui_{tid}.jsonl").write_text("", encoding="utf-8")
    (threads / f"meta_tui_{tid}.json").write_text("{}", encoding="utf-8")

    deleted = delete_thread(str(threads), "tui", tid)
    assert str(touched) in deleted
    assert str(legacy) in deleted
    assert not touched.exists()
    assert not legacy.exists()
