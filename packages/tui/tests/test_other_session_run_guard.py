"""Ход другой сессии (решение 24): агент один на приложение. Пока идёт
ход сессии B, в открытой A Enter не отправляет (текст в поле, отказ в
слоте футера), Esc и Ctrl+C ход B не останавливают и откат не делают."""

from __future__ import annotations

import json

import pytest

pytest.importorskip("textual")

from footer_text import footer_rows
from gc_testkit.tui_support import tui_settings
from gc_tui.app import TuiApp
from gc_tui.services.footer_slot import OTHER_SESSION_RUN_REFUSAL
from gc_tui.widgets.command_input import CommandTextArea
from gc_tui.widgets.user_reply import UserReplyContainer

_A = "aaaa0000000000000000000000000003a"
_B = "bbbb0000000000000000000000000004b"


def _write(tmp_path, tid, records=()):
    meta = {"thread_id": tid, "system_id": "cli", "tui": {}}
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    lines = [
        json.dumps(
            {
                "type": "message",
                "seq": i,
                "ts_utc": "2026-10-22T10:00:00+00:00",
                "payload": p,
            }
        )
        for i, p in enumerate(records, 1)
    ]
    (tmp_path / f"data_cli_{tid}.jsonl").write_text(
        "".join(line + "\n" for line in lines), encoding="utf-8"
    )


def _meta(tmp_path, tid):
    path = tmp_path / f"meta_cli_{tid}.json"
    return json.loads(path.read_text(encoding="utf-8")).get("tui") or {}


class _Runner:
    """Один раннер приложения; ход идёт в треде `target`."""

    _thread_kind = "run"

    def __init__(self, target):
        self._last_target = (target, "cli")
        self.running = True
        self.added: list[dict] = []
        self.withdrawn: list[str] = []
        self.started: list[dict] = []
        self.stops = 0

    def is_running(self):
        return self.running

    def stop(self):
        self.stops += 1
        self.running = False

    def cancel_confirm(self):
        pass

    def is_child_input_active(self):
        return False

    def add_message(self, message):
        self.added.append(message)

    def withdraw_message(self, msg_id):
        self.withdrawn.append(msg_id)
        return True

    def start(self, **kwargs):
        self.started.append(kwargs)
        self._last_target = (kwargs["thread_id"], kwargs["system"])
        self.running = True
        return True


def _app(tmp_path):
    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _b_runs_a_open(pilot, tmp_path):
    """B идёт (ход застал тик), человек открыл A."""
    scr = pilot.app.screen
    scr.attach_active_task(_B, is_resume=True)
    runner = _Runner(_B)
    pilot.app.active_runner = runner
    scr._poll_active()
    scr.attach_active_task(_A, is_resume=True)
    await pilot.pause()
    return scr, runner


def _inp(scr):
    return scr.query_one("#cmdinput", CommandTextArea)


def _replies(scr):
    return list(scr.query(UserReplyContainer))


@pytest.mark.asyncio
async def test_enter_refused_text_kept_nothing_queued(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        inp = _inp(scr)
        inp.focus()
        inp.text = "почини тест"
        await pilot.press("enter")
        await pilot.pause()
        assert inp.text == "почини тест"
        assert runner.added == []
        assert runner.started == []
        assert _replies(scr) == []
        assert not _meta(tmp_path, _A).get("pending_user_messages")
        assert OTHER_SESSION_RUN_REFUSAL in footer_rows(scr)[1]
        assert runner.running is True


@pytest.mark.asyncio
async def test_skill_with_text_refused_and_kept(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        await scr._cmd_use_skill("p:coder", prompt="почини тест")
        await pilot.pause()
        assert _inp(scr).text == "/p:coder почини тест"
        assert runner.added == []
        assert _replies(scr) == []
        assert OTHER_SESSION_RUN_REFUSAL in footer_rows(scr)[1]


@pytest.mark.asyncio
async def test_esc_and_ctrl_c_do_not_stop_other_run(tmp_path):
    _write(tmp_path, _A, [{"id": "u1", "role": "user", "content": "вопрос А"}])
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        before = [r.plain_text for r in _replies(scr)]
        assert before == ["вопрос А"]
        _inp(scr).focus()
        await pilot.press("escape")
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        pilot.app.action_interrupt()
        await pilot.pause()
        assert runner.stops == 0
        assert runner.running is True
        assert pilot.app.active_runner is runner
        assert [r.plain_text for r in _replies(scr)] == before
        assert _inp(scr).text == ""
        assert OTHER_SESSION_RUN_REFUSAL in footer_rows(scr)[1]


@pytest.mark.asyncio
async def test_after_other_run_ends_enter_starts_own_run(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        runner.running = False
        scr._poll_active()
        inp = _inp(scr)
        inp.focus()
        inp.text = "теперь A"
        await pilot.press("enter")
        await pilot.pause()
        assert inp.text == ""
        (started,) = runner.started
        assert started["thread_id"] == _A
        assert started["query"].endswith("теперь A")


@pytest.mark.asyncio
async def test_back_to_running_session_queue_and_up_work(tmp_path):
    _write(tmp_path, _A)
    _write(tmp_path, _B)
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        inp = _inp(scr)
        inp.focus()
        inp.text = "ещё к B"
        await pilot.press("enter")
        await pilot.pause()
        assert [m["content"] for m in runner.added] == ["ещё к B"]
        assert [r.state for r in _replies(scr)] == ["queued"]
        await pilot.press("up")
        await pilot.pause()
        assert inp.text == "ещё к B"
        assert runner.withdrawn == [runner.added[0]["id"]]


@pytest.mark.asyncio
async def test_switch_disables_undo_of_previous_send(tmp_path):
    """Отправка была в B; после перехода на A и обратно Esc не откатывает
    реплику — счётчик событий после отправки принадлежал прежнему виду."""
    _write(tmp_path, _A)
    _write(tmp_path, _B, [{"id": "u1", "role": "user", "content": "к B"}])
    async with _app(tmp_path).run_test() as pilot:
        scr, runner = await _b_runs_a_open(pilot, tmp_path)
        scr._events_since_last_user_send = 0  # отправка только что в B
        scr.attach_active_task(_B, is_resume=True)
        await pilot.pause()
        assert scr._can_cancel_with_undo() is False
