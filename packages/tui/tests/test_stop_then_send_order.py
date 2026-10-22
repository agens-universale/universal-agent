"""Стоп и сразу отправка: новый ход ждёт, пока остановленный закроется.

Остановленный раннер дописывает запись итога в своём `finally`, уже
отвязанным. Новый ход, начатый во время мигания `■`, ждёт его (не на
UI-потоке): в файле треда запись стопа встаёт до реплики нового хода, и
строка «■ Отменено» — над новой репликой, вживую и после переоткрытия.
"""

from __future__ import annotations

import asyncio
import json
import threading

import pytest

pytest.importorskip("textual")

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings, wait_until

TID = "stopsend0000000000000000000000001a"
OTHER = "stopsend0000000000000000000000002b"
OLD_TEXT = "долгая задача"
NEW_TEXT = "другая задача"
# Сколько остановленный ход дописывает после стопа: меньше ожидания
# нового хода (`STOP_SETTLE_SEC`), но заметно дольше старта нового.
_STOP_TAIL_SEC = 0.6


def _write_meta(tmp_path, tid):
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "system_id": "cli"}), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _records(tmp_path):
    path = tmp_path / f"data_cli_{TID}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _order(tmp_path) -> list[str]:
    """Порядок файла: `id` сообщений, `rec:<opened_by>` записей итога."""
    out = []
    for r in _records(tmp_path):
        if r.get("type") == "turn_outcome":
            out.append(f"rec:{r['outcome']['opened_by']}")
        else:
            out.append(r["payload"]["id"])
    return out


class _Thread:
    """Файл треда, как его пишет ядро; seq — общий у обоих ходов."""

    def __init__(self, threads_dir):
        self.dir = str(threads_dir)
        self.seq = 0
        self.lock = threading.Lock()

    def messages(self, *payloads):
        with self.lock:
            append_new_thread_messages(
                self.dir,
                "cli",
                TID,
                list(payloads),
                set(),
                start_seq=self.seq,
            )
            self.seq += len(payloads)

    def record(self, opened_by, status, reason=None):
        outcome = TurnOutcome(
            status=status,
            reason=reason,
            opened_by=opened_by,
            started_at="2026-10-22T11:30:00Z",
            ended_at="2026-10-22T11:30:12Z",
            duration_sec=12.0,
            error=None,
        )
        with self.lock:
            append_turn_outcome_record(
                self.dir, "cli", TID, outcome, seq=self.seq
            )


class _Executor:
    """Свой у каждого раннера. Первый ход ждёт стопа и дописывает запись
    стопа с задержкой (поток уже отвязан); следующий — сразу до конца."""

    def __init__(self, thread: _Thread, *, blocking: bool):
        self.thread = thread
        self.blocking = blocking
        self.stopped = threading.Event()

    async def arun(self, messages, *, message_ids=None, **_kw):
        mid = message_ids[0]
        self.thread.messages(
            {"id": mid, "role": "user", "content": messages[0]},
            {
                "id": f"a-{mid}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ответ"}],
            },
        )
        if not self.blocking:
            self.thread.record(mid, "done")
            return
        try:
            await asyncio.to_thread(self.stopped.wait, 10)
        finally:
            await asyncio.sleep(_STOP_TAIL_SEC)
            self.thread.record(mid, "stopped", "user_stop")

    def stop_run(self):
        self.stopped.set()

    def add_message(self, _m):
        pass

    def turn_effective_start(self):
        return None


def _app(tmp_path):
    from gc_tui.app import TuiApp

    thread = _Thread(tmp_path)
    made: list[_Executor] = []

    def factory():
        made.append(_Executor(thread, blocking=not made))
        return made[-1]

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    app._executor_factory = factory
    return app


def _trace(scr) -> list[str]:
    """Видимый порядок ленты: реплики и осевшие строки ходов."""
    from gc_tui.widgets.turn_line import TurnLine
    from gc_tui.widgets.user_reply import UserReplyContainer

    out = []
    for w in scr.query_one("#trace").children:
        if isinstance(w, TurnLine) and not w.has_class("-latest"):
            out.append(f"line:{w.opened_by}")
        elif isinstance(w, UserReplyContainer):
            out.append(f"› {w.plain_text}")
    return out


async def _send(pilot, text):
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.press("enter")
    await pilot.pause()
    return pilot.app.screen._last_user_message_id


@pytest.mark.asyncio
async def test_new_turn_after_stop_waits_for_stopped_record(tmp_path):
    _write_meta(tmp_path, TID)
    _write_meta(tmp_path, OTHER)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        old = await _send(pilot, OLD_TEXT)
        await wait_until(
            pilot,
            lambda: scr._events_since_last_user_send > 0,
            "ответ первого хода в ленте",
        )
        scr._cmd_stop()
        await pilot.pause()
        assert scr._stopping_alive()  # мигает «■»: поток дописывает
        new = await _send(pilot, NEW_TEXT)
        await wait_until(
            pilot,
            lambda: (
                f"rec:{new}" in _order(tmp_path)
                and not pilot.app.active_runner.is_running()
            ),
            "новый ход закрыт",
        )
        await wait_until(
            pilot,
            lambda: (
                scr._last_turn_outcome is not None
                and scr._last_turn_outcome.opened_by == new
            ),
            "итог нового хода прочитан",
        )
        assert _order(tmp_path) == [
            old,
            f"a-{old}",
            f"rec:{old}",
            new,
            f"a-{new}",
            f"rec:{new}",
        ]
        expected = [f"› {OLD_TEXT}", f"line:{old}", f"› {NEW_TEXT}"]
        assert _trace(scr) == expected
        assert scr._last_turn_outcome.status == "done"
        scr.attach_active_task(OTHER, is_resume=True)
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _trace(scr) == expected
        assert scr._last_turn_outcome.opened_by == new
