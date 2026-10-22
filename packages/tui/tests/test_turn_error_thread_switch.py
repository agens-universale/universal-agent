"""Исход хода принадлежит треду, чей прогон кончился: переключение на
другую сессию не переносит туда ни `✕ Ошибка`, ни её причину."""

from __future__ import annotations

import json
import time

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.widgets.thinking_indicator import is_terminal_indicator_state

_A = "aaaa0000000000000000000000000001a"
_B = "bbbb0000000000000000000000000002b"
_REASON = "MainModelUnavailable: модель недоступна"


def _write(tmp_path, tid, tui):
    meta = {"thread_id": tid, "system_id": "cli", "tui": tui}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _meta(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    return json.loads(path.read_text(encoding="utf-8"))["tui"]


class _Runner:
    """Прогон треда B: на момент опроса уже упал."""

    _thread_kind = "run"

    def __init__(self, target):
        self._last_target = target
        self.run_started_at = time.monotonic() - 7
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
        {
            "status": "completed",
            "last_indicator": {"kind": "done", "elapsed": 5},
        },
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
        assert _meta(tmp_path, _B)["last_error"] == _REASON
        assert _meta(tmp_path, _B)["last_indicator"] == {
            "kind": "failed",
            "elapsed": None,
            "edits": None,
            "plan": None,
        }
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        text = scr._thinking._render_text().plain
        assert text == f"✕ Ошибка\n  └ {_REASON}"


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _write_pair(tmp_path):
    _write(
        tmp_path,
        _A,
        {
            "status": "completed",
            "last_indicator": {"kind": "done", "elapsed": 5},
        },
    )
    _write(tmp_path, _B, {"status": "running"})


@pytest.mark.asyncio
async def test_running_other_thread_shows_no_activity_here(tmp_path):
    pytest.importorskip("textual")
    _write_pair(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_B, is_resume=True)
        runner = _Runner((_B, "cli"))
        pilot.app.active_runner = runner
        scr._poll_active()
        assert not is_terminal_indicator_state(scr._thinking._state)
        scr.attach_active_task(_A, is_resume=True)
        scr._poll_active()
        await pilot.pause()
        plain = scr._thinking._render_text().plain
        assert plain.startswith("✓ Выполнил")
        assert _meta(tmp_path, _A)["status"] == "completed"
        # возврат к B: его прогон идёт — индикатор активности на месте
        scr.attach_active_task(_B, is_resume=True)
        scr._poll_active()
        await pilot.pause()
        assert not scr._thinking._render_text().plain.startswith("✓")
        assert _meta(tmp_path, _B)["status"] == "running"
        # B кончился, пока человек на A: исход в meta B, A не тронут
        scr.attach_active_task(_A, is_resume=True)
        runner._running = False
        scr._poll_active()
        await pilot.pause()
        assert _meta(tmp_path, _B)["status"] == "completed"
        assert _meta(tmp_path, _B)["last_indicator"] == {
            "kind": "done",
            "elapsed": 7,
            "edits": None,
            "plan": None,
        }
        assert _meta(tmp_path, _A)["last_indicator"] == {
            "kind": "done",
            "elapsed": 5,
        }
        assert scr._thinking._render_text().plain.startswith("✓ Выполнил")
