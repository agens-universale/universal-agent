"""Переход из сессии с идущим ходом (решение 25): только через вопрос
«остановить и перейти». Остаться — ничего не меняется; остановить — ход
кончается, его исход ложится в его тред, и только потом переход."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.screens.confirm import ConfirmScreen
from gc_tui.screens.sessions import SessionsScreen

_A = "gate0000000000000000000000000000a"
_B = "gate0000000000000000000000000000b"
_QUESTION = "В этой сессии идёт ход. Остановить его и перейти?"


def _write(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _status(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    meta = json.loads(path.read_text(encoding="utf-8"))
    return (meta.get("tui") or {}).get("status")


class _Runner:
    """Ход треда `tid`. `slow` — стоп кооперативный: поток ещё жив, пока
    тест не «доведёт» ход до конца."""

    _thread_kind = "run"

    def __init__(self, tid, *, slow=False):
        self._last_target = (tid, "cli")
        self._stop_requested = False
        self.run_started_at = None
        self.running = True
        self.slow = slow
        self.stops = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self._stop_requested = True
        if not self.slow:
            self.running = False

    def cancel_confirm(self):
        pass

    def cancel_ask_human(self):
        pass

    def withdraw_message(self, msg_id):
        return False


def _app(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _run_in_a(pilot, *, slow=False):
    scr = pilot.app.screen
    scr.attach_active_task(_A, is_resume=True)
    scr._prepare_for_new_run()
    runner = _Runner(_A, slow=slow)
    pilot.app.active_runner = runner
    scr._poll_active()  # тик застал ход
    await pilot.pause()
    return scr, runner


def _dialog(pilot):
    found = [s for s in pilot.app.screen_stack if isinstance(s, ConfirmScreen)]
    return found[0] if found else None


async def _pick_b(pilot, tmp_path):
    """/sessions → выбрать B (Enter по карточке)."""
    await pilot.app.push_screen(
        SessionsScreen(threads_dir=str(tmp_path), system="cli")
    )
    await pilot.pause()
    pilot.app.screen._resume_thread(_B)
    await pilot.pause()


def _new(scr):
    scr._cmd_new()


async def _switch(pilot, tmp_path, path):
    if path == "new":
        _new(pilot.app.screen)
    else:
        await _pick_b(pilot, tmp_path)
    await pilot.pause()


_TARGET = {"new": None, "sessions": _B}


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_no_run_switches_at_once(tmp_path, path):
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(_A, is_resume=True)
        await _switch(pilot, tmp_path, path)
        assert _dialog(pilot) is None
        assert scr._active_task_id == _TARGET[path]


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_run_asks_and_stay_changes_nothing(tmp_path, path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await _switch(pilot, tmp_path, path)
        dialog = _dialog(pilot)
        assert dialog is not None
        assert dialog._message == _QUESTION
        assert [label for _key, label in dialog._options] == [
            "Остановить и перейти",
            "Остаться",
        ]
        await pilot.press("n")
        await pilot.pause()
        scr._poll_active()
        assert scr._active_task_id == _A
        assert runner.stops == 0
        assert runner.is_running()
        assert pilot.app.active_runner is runner
        assert _status(tmp_path, _A) == "running"


@pytest.mark.asyncio
@pytest.mark.parametrize("path", ["new", "sessions"])
async def test_stop_waits_for_run_end_then_switches(tmp_path, path):
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot, slow=True)
        opened = []
        attach = scr.attach_active_task
        empty = scr._switch_to_empty_state

        def _spy_attach(tid, **kw):
            opened.append((tid, runner.is_running()))
            return attach(tid, **kw)

        def _spy_empty():
            opened.append((None, runner.is_running()))
            return empty()

        scr.attach_active_task = _spy_attach
        scr._switch_to_empty_state = _spy_empty
        await _switch(pilot, tmp_path, path)
        await pilot.press("y")
        await pilot.pause()
        assert runner.stops == 1
        # Ход ещё жив (стоп кооперативный): сессия не сменилась.
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _A
        assert opened == []
        runner.running = False  # ход дошёл до конца
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _TARGET[path]
        assert opened == [(_TARGET[path], False)]
        assert _status(tmp_path, _A) == "stopped"


@pytest.mark.asyncio
async def test_resume_from_preview_goes_through_gate(tmp_path):
    """Превью → «продолжить»: тот же вопрос."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        await pilot.app.push_screen(
            SessionsScreen(threads_dir=str(tmp_path), system="cli")
        )
        await pilot.pause()
        sessions = pilot.app.screen
        sessions._resume_thread(_B)
        await pilot.pause()
        assert _dialog(pilot) is not None
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _B
        assert _status(tmp_path, _A) == "stopped"


@pytest.mark.asyncio
async def test_stop_and_switch_clears_question_of_old_run(tmp_path):
    """Ход ждал ответа человека: после перехода вопроса прежней сессии нет."""
    from gc_tui.state import AskHumanState

    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        from textual.widgets import Static

        banner = Static("?")
        await scr.mount(banner)
        scr._ask_human_banner = banner
        pilot.app.tui_state.ask_human_state = AskHumanState(
            question="?", call_id="c1"
        )
        _new(scr)
        await pilot.pause()
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id is None
        assert pilot.app.tui_state.ask_human_state is None


@pytest.mark.asyncio
async def test_stop_and_switch_clears_confirm_of_old_run(tmp_path):
    """Ход ждал подтверждения: баннер прежней сессии в новую не едет."""
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _run_in_a(pilot)
        scr._confirm_active = True
        await _pick_b(pilot, tmp_path)
        await pilot.press("y")
        await pilot.pause()
        scr._poll_active()
        await pilot.pause()
        assert scr._active_task_id == _B
        assert scr._confirm_active is False
