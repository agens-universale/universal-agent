"""Исход хода принадлежит треду, чей прогон кончился: переключение на
другую сессию не переносит туда ни `✕ Ошибка`, ни её причину. Причина —
в записи итога хода треда (её пишет ядро), не в мете."""

from __future__ import annotations

import json

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings

_A = "aaaa0000000000000000000000000001a"
_B = "bbbb0000000000000000000000000002b"
_REASON = "MainModelUnavailable: модель недоступна"


def _write(tmp_path, tid, tui):
    meta = {"thread_id": tid, "system_id": "cli", "tui": tui}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _failed_turn(tmp_path, tid):
    """Ход треда упал: реплика и запись итога с причиной."""
    append_new_thread_messages(
        str(tmp_path),
        "cli",
        tid,
        [{"id": "u1", "role": "user", "content": "q"}],
        set(),
    )
    outcome = TurnOutcome(
        status="error",
        reason=None,
        opened_by="u1",
        started_at="2026-10-22T11:32:00Z",
        ended_at="",
        duration_sec=7.0,
        error={"type": "MainModelUnavailable", "message": _REASON},
    )
    append_turn_outcome_record(str(tmp_path), "cli", tid, outcome, seq=1)


def _meta(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    return json.loads(path.read_text(encoding="utf-8"))["tui"]


class _Runner:
    """Прогон треда B: на момент опроса уже упал."""

    _thread_kind = "run"

    def __init__(self, target):
        self._last_target = target
        self._running = True

    def is_running(self):
        return self._running

    def stop(self):
        pass


@pytest.mark.asyncio
async def test_failure_after_switch_stays_with_failed_thread(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    _write(
        tmp_path,
        _A,
        {"status": "completed", "last_indicator": {"edits": None}},
    )
    _write(tmp_path, _B, {"status": "running"})
    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_B, is_resume=True)
        runner = _Runner((_B, "cli"))
        pilot.app.active_runner = runner
        scr._poll_active()  # тик застал прогон B
        # B упал; до следующего тика человек уходит на A.
        _failed_turn(tmp_path, _B)
        runner._running = False
        pilot.app.last_runner_error = _REASON
        scr.attach_active_task(_A, is_resume=True)
        scr._poll_active()
        await pilot.pause()
        assert scr._thinking._render_text().plain.startswith("✓ Выполнил")
        assert scr._thinking.error_rows == 0
        assert "last_error" not in _meta(tmp_path, _A) or not _meta(
            tmp_path, _A
        ).get("last_error")
        assert _meta(tmp_path, _A)["status"] == "completed"
        assert "last_error" not in _meta(tmp_path, _B)
        assert _meta(tmp_path, _B)["status"] == "failed"
        assert _meta(tmp_path, _B)["last_indicator"] == {"edits": None}
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        text = scr._thinking._render_text().plain
        assert text == f"✕ Ошибка\n  └ {_REASON}"
