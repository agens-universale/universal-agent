"""Покой после `/compact` (S10 §4.9): строка итога хода — над маркером
сжатия, свежая, со сводкой правок справа; бара нет.

Тред пишется настоящими функциями `gc_core.thread_store`: ход закрыт
записью итога, затем сжатие, как у ядра (живой файл — в архив, новый —
со сводкой, мета с эпохой сжатия). Часы и пояс кадра — общие для
эталонов (`pinned_snapshot_clock`).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from gc_testkit.tui_support import SNAPSHOT_NOW, compact_thread, tui_settings

pytest.importorskip("textual")
pytest.importorskip("pytest_textual_snapshot")

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.develop_files import (
    append_new_thread_messages,
)
from gc_testkit.thread_writes import turn_outcome_record

_SYS = "cli"
_TID = "7f3a9b1c"
# Конец хода — за 8 минут до «сейчас» эталонов (`pinned_snapshot_clock`).
_ENDED = SNAPSHOT_NOW - timedelta(minutes=8)


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
    turn_outcome_record(str(tmp_path), _SYS, _TID, outcome)


def _compact(tmp_path) -> None:
    summary = (
        '<technical type="dialog_summary">'
        "<summary>таймаут клиента поднят до 30 с</summary></technical>"
    )
    compact_thread(
        str(tmp_path),
        _SYS,
        _TID,
        [{"id": "s1", "role": "user", "content": summary}],
    )


def test_turn_line_compaction_snapshot(snap_compare, tmp_path, monkeypatch):
    from gc_tui.app import TuiApp
    from gc_tui.screens.main import TraceScreen

    monkeypatch.setattr(
        TraceScreen, "_current_cwd", lambda self: "/work/demo-project"
    )
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
