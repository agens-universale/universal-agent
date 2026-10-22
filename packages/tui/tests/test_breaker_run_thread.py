"""Предохранитель моделей принадлежит треду прогона (спека §8): трип хода
сессии B, пока открыта A, пишется в meta B и не попадает ни в meta, ни в
предохранитель A."""

from __future__ import annotations

import json
import threading

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.app import TuiApp
from gc_tui.session_settings import BREAKER_META_KEY

_A = "aaaa0000000000000000000000000005a"
_B = "bbbb0000000000000000000000000006b"


def _write(tmp_path, tid):
    meta = {"thread_id": tid, "system_id": "cli", "tui": {}}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _tui(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    return json.loads(path.read_text(encoding="utf-8")).get("tui") or {}


class _Exec:
    """Ход B: трип алиаса `flash` до или после ухода человека на A."""

    last_terminal_outcome = "finished"

    def __init__(self, app, *, trip_early):
        self._app = app
        self._trip_early = trip_early
        self.started = threading.Event()
        self.release = threading.Event()

    def add_message(self, m):
        pass

    async def arun(self, *a, **k):
        if self._trip_early:
            self._app._circuit_breaker.trip("flash", "недоступна")
        self.started.set()
        self.release.wait(timeout=5)
        if not self._trip_early:
            self._app._circuit_breaker.trip("flash", "недоступна")
        return "done"

    def stop_run(self):
        self.release.set()


async def _b_runs_then_a_opens(pilot, ex):
    app = pilot.app
    scr = app.screen
    scr.attach_active_task(_B, is_resume=True)
    runner = app.get_runner()
    runner.start(query="q", thread_id=_B, system="cli")
    await wait_until(pilot, ex.started.is_set, "ход B начался")
    await pilot.pause(0.3)  # тик застал прогон B
    scr.attach_active_task(_A, is_resume=True)
    await pilot.pause()
    return scr, runner, ex


async def _b_finishes(pilot, runner, ex):
    ex.release.set()
    await wait_until(pilot, lambda: not runner.is_running(), "конец хода B")
    await pilot.pause(0.5)  # тик спадающего фронта


def _app(tmp_path):
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


@pytest.mark.asyncio
@pytest.mark.parametrize("trip_early", [False, True])
async def test_trip_of_other_session_stays_with_it(tmp_path, trip_early):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    app = _app(tmp_path)
    ex = _Exec(app, trip_early=trip_early)
    # Раннер строится с этой фабрикой и на прогреве при старте окна.
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        scr, runner, ex = await _b_runs_then_a_opens(pilot, ex)
        # Пока B идёт, его предохранитель живой: трип до ухода не стёрт.
        assert app._circuit_breaker.is_open("flash") is trip_early
        await _b_finishes(pilot, runner, ex)
        assert BREAKER_META_KEY not in _tui(tmp_path, _A)
        assert _tui(tmp_path, _B)[BREAKER_META_KEY] == {"flash": "недоступна"}
        assert app._circuit_breaker.is_open("flash") is False
        scr.attach_active_task(_A, is_resume=True)
        await pilot.pause()
        assert app._circuit_breaker.snapshot() == {}
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        assert app._circuit_breaker.is_open("flash") is True
