"""Покой после `/compact` (S10 §4.9): строка итога хода — над маркером
сжатия, свежая, со сводкой правок справа; бара нет.

Тред пишется настоящими функциями `gc_core.thread_store`: ход закрыт
записью итога, затем сжатие, как у ядра (живой файл — в архив, новый —
со сводкой, мета с эпохой сжатия). Время закреплено: конец хода — на
местных часах, «сейчас» — подмена `relative_time._now_local`.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
    archive_and_replace_thread_files,
    load_thread_meta,
    save_thread_meta,
)

_SYS = "cli"
_TID = "7f3a9b1c"
_ENDED = datetime(2026, 10, 22, 14, 32).astimezone()
_SUFFIX = "20261022_143500"


def _iso(moment: datetime) -> str:
    return moment.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _write_thread(tmp_path) -> None:
    meta = {
        "thread_id": _TID,
        "system_id": _SYS,
        "tui": {
            "status": "completed",
            "cwd": "/work/project",
            "last_indicator": {"edits": [2, 14, 3]},
        },
    }
    (tmp_path / f"meta_{_SYS}_{_TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    append_new_thread_messages(
        str(tmp_path),
        _SYS,
        _TID,
        [
            {"id": "u1", "role": "user", "content": "подними таймаут"},
            {"id": "a1", "role": "assistant", "content": "Готово: 30 с."},
        ],
        set(),
    )
    outcome = TurnOutcome(
        status="done",
        reason=None,
        opened_by="u1",
        started_at=_iso(_ENDED - timedelta(seconds=72)),
        ended_at=_iso(_ENDED),
        duration_sec=72.0,
        error=None,
    )
    append_turn_outcome_record(str(tmp_path), _SYS, _TID, outcome, seq=2)


def _compact(tmp_path) -> None:
    summary = (
        '<technical type="dialog_summary">'
        "<summary>таймаут клиента поднят до 30 с</summary></technical>"
    )
    meta = load_thread_meta(str(tmp_path), _SYS, _TID)
    archive_and_replace_thread_files(
        str(tmp_path),
        _SYS,
        _TID,
        [{"id": "s1", "role": "user", "content": summary}],
        archive_suffix=_SUFFIX,
    )
    save_thread_meta(
        str(tmp_path),
        _SYS,
        _TID,
        {**meta, "last_summary_archive_suffix": _SUFFIX},
    )


def test_turn_line_compaction_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen
    from gc_tui.services import relative_time

    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
    now = _ENDED + timedelta(minutes=5)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)
    _write_thread(tmp_path)

    app = TuiApp(
        threads_dir=str(tmp_path),
        system=_SYS,
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
    )

    async def setup(pilot):
        await pilot.pause()
        scr = pilot.app.screen
        scr.attach_active_task(_TID, is_resume=True)
        await pilot.pause()
        _compact(tmp_path)  # `/compact` после хода: шаг сжатия хвостом
        scr._poll_trace_tail()
        scr.query_one("#cmdinput").cursor_blink = False
        await pilot.pause(0.5)

    assert snap_compare(app, terminal_size=(100, 30), run_before=setup)
