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
from types import SimpleNamespace

import pytest

pytest.importorskip("textual")

from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.thread_writes import turn_outcome_record
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
    from gc_tui.services.stores.file_store import FileTaskStore

    return FileTaskStore(str(tmp_path)).read_history(TID)[0][-1]["messages"]


def _order(tmp_path) -> list[str]:
    """Порядок ленты: `id` сообщений, `rec:<opened_by>` записей итога (по
    привязке к сообщению хода)."""
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
            turn_outcome_record(self.dir, "cli", TID, outcome)


class _Executor:
    """Свой у каждого раннера. Первый ход ждёт стопа и дописывает запись
    стопа с задержкой (поток уже отвязан); следующий — сразу до конца."""

    def __init__(self, thread: _Thread, *, blocking: bool):
        self.thread = thread
        self.blocking = blocking
        self.stopped = threading.Event()
        #: Состояние прогона, как у ядра: флаг «прогон что-то выдал».
        self.state = None

    def get_state(self):
        return self.state

    async def arun(self, messages, *, message_ids=None, **_kw):
        mid = message_ids[0]
        self.state = SimpleNamespace(produced_output=False)
        self.thread.messages(
            {"id": mid, "role": "user", "content": messages[0]},
            {
                "id": f"a-{mid}",
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "ответ"}],
            },
        )
        self.state.produced_output = True
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
            lambda: pilot.app.active_runner.produced_output(),
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
                scr._feed_history.last_turn_outcome is not None
                and scr._feed_history.last_turn_outcome.opened_by == new
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
        assert scr._feed_history.last_turn_outcome.status == "done"
        scr.attach_active_task(OTHER, is_resume=True)
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _trace(scr) == expected
        assert scr._feed_history.last_turn_outcome.opened_by == new


@pytest.mark.asyncio
async def test_record_read_after_send_goes_above_sent_reply(tmp_path):
    """Запись треда, дочитанная после отправки новой реплики (ответ
    старого хода, вызов и его вывод), встаёт над отправленной, ещё не
    записанной репликой: в файле она раньше реплики — как строка итога и
    как после переоткрытия."""
    from feed_dump import HISTORY_THREAD, feed_live
    from gc_tui.widgets.user_reply import UserReplyContainer
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_steps import _giant_turn

    records = _giant_turn(0, 1, outcome=False)
    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(scr, records[:1], splits=())
        sent = scr._mount_user_reply("новая", state="pending", client_id="c")
        feed_live(scr, records[1:], splits=())
        await pilot.pause()
        children = list(scr.query_one("#trace").children)
        assert children[-1] is sent
        assert isinstance(sent, UserReplyContainer)
        assert sent.state == "pending"


def _index_of(scr, text: str) -> int:
    """Место в ленте первой строки с текстом ``text``."""
    from feed_dump import feed_dump

    rows = feed_dump(scr.query_one("#trace"))
    return next(i for i, row in enumerate(rows) if text in row[1])


@pytest.mark.asyncio
async def test_record_goes_above_sent_reply_past_unpinned_row(tmp_path):
    """Под отправленной репликой встала строка не из модели (вывод «!»,
    служебная строка) — запись, дочитанная после отправки, всё равно
    встаёт над репликой: правило не обрывается на первой незакреплённой
    строке с конца."""
    from feed_dump import HISTORY_THREAD, feed_live
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_steps import _giant_turn

    records = _giant_turn(0, 1, outcome=False)
    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(scr, records[:1], splits=())
        scr._mount_user_reply("новая", state="pending", client_id="c")
        scr._mount_trace_row(
            prefix="", content="служебная строка", kind_class="line-meta"
        )
        feed_live(scr, records[1:], splits=())
        await pilot.pause()
        sent, note = _index_of(scr, "новая"), _index_of(scr, "служебная")
        assert _index_of(scr, "Ход 0: смотрю шаг 1.") < sent < note


@pytest.mark.asyncio
async def test_unmatched_user_record_releases_sent_reply(tmp_path):
    """Реплика пользователя дочитана, но ни с одной отправленной не
    сверилась (wire другой): отправленная реплика больше не держит
    отрисовку модели над собой — иначе она навсегда последняя в ленте, а
    все следующие ходы встают над ней."""
    from feed_dump import HISTORY_THREAD, feed_live
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_steps import _giant_turn

    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        sent = scr._mount_user_reply("новая", state="pending", client_id="c")
        feed_live(scr, _giant_turn(0, 1), splits=())
        feed_live(scr, _giant_turn(1, 1, outcome=False), splits=())
        await pilot.pause()
        assert sent.state == "pending"
        assert _index_of(scr, "новая") < _index_of(scr, "Ход 1: смотрю шаг 1.")


@pytest.mark.asyncio
async def test_ask_human_answer_does_not_release_sent_reply(tmp_path):
    """Ответ на вопрос агента — тоже запись `user`, но не реплика хода: не
    сверившись с отправленной (wire другой — плашки картинок), он её не
    отпускает. Запись модели после ответа — над отправленной репликой."""
    from feed_dump import HISTORY_THREAD, feed_live
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_far_edge import _message
    from test_trace_window_steps import _giant_turn, _prose

    question = [
        _message(
            {
                "type": "function_call",
                "name": "ask_human",
                "call_id": "q1",
                "id": "fc_q1",
                "status": "completed",
                "arguments": json.dumps({"question": "какой файл?"}),
            }
        ),
    ]
    answer = _message(
        {
            "type": "function_call_output",
            "name": "ask_human",
            "call_id": "q1",
            "id": "fco_q1",
            "output": json.dumps(
                {"tool": "ask_human", "ok": True, "response": "этот"}
            ),
        }
    )
    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(
            scr, [*_giant_turn(0, 1, outcome=False)[:1], *question], splits=()
        )
        scr._mount_user_reply("новая", state="pending", client_id="c")
        feed_live(scr, [answer, _prose(0, 7)], splits=())
        await pilot.pause()
        assert _index_of(scr, "Ход 0: смотрю шаг 7.") < _index_of(scr, "новая")


@pytest.mark.asyncio
async def test_unmatched_answer_reconciles_by_question(tmp_path):
    """Ответ на вопрос агента, отправленный этим экраном, не сверился по
    wire (ядро дописало плашку картинки): он сверяется по вопросу
    (call_id) — отправленная реплика ответа становится записанной, второй
    реплики нет (R48 M5)."""
    from feed_dump import HISTORY_THREAD, feed_live
    from gc_tui.widgets.user_reply import UserReplyContainer
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_far_edge import _message
    from test_trace_window_steps import _giant_turn

    question = _message(
        {
            "type": "function_call",
            "name": "ask_human",
            "call_id": "q1",
            "id": "fc_q1",
            "status": "completed",
            "arguments": json.dumps({"question": "какой файл?"}),
        }
    )
    answer = _message(
        {
            "type": "function_call_output",
            "name": "ask_human",
            "call_id": "q1",
            "id": "fco_q1",
            "output": json.dumps(
                {"tool": "ask_human", "ok": True, "response": "[#1] этот"}
            ),
        }
    )
    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(
            scr, [*_giant_turn(0, 1, outcome=False)[:1], question], splits=()
        )
        scr._ask_human_call_id = "q1"
        cid = scr._track_pending(display_text="этот", wire_content="этот")
        scr._track_answer(cid)
        sent = scr._mount_user_reply("этот", state="pending", client_id=cid)
        feed_live(scr, [answer], splits=())
        await pilot.pause()
        replies = list(scr.query_one("#trace").query(UserReplyContainer))
        assert sent.state == "normal"
        assert [r.plain_text for r in replies].count("этот") == 1
        assert not any("[#1] этот" in r.plain_text for r in replies)


@pytest.mark.asyncio
async def test_answer_reconciles_before_same_text_queued_reply(tmp_path):
    """Раньше отправлена реплика «да», и ответ на вопрос — тоже «да»:
    запись ответа сверяется с отправленным ответом (по вопросу), а не с
    первой по порядку отправленной, хотя wire совпал у обеих (R49 m3)."""
    from feed_dump import HISTORY_THREAD, feed_live
    from test_trace_ephemera import _SIZE
    from test_trace_ephemera import _app as _ephemera_app
    from test_trace_window_far_edge import _message
    from test_trace_window_steps import _giant_turn

    question = _message(
        {
            "type": "function_call",
            "name": "ask_human",
            "call_id": "q1",
            "id": "fc_q1",
            "status": "completed",
            "arguments": json.dumps({"question": "продолжать?"}),
        }
    )
    answer = _message(
        {
            "type": "function_call_output",
            "name": "ask_human",
            "call_id": "q1",
            "id": "fco_q1",
            "output": json.dumps(
                {"tool": "ask_human", "ok": True, "response": "да"}
            ),
        }
    )
    async with _ephemera_app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        feed_live(
            scr, [*_giant_turn(0, 1, outcome=False)[:1], question], splits=()
        )
        queued_cid = scr._track_pending(display_text="да", wire_content="да")
        queued = scr._mount_user_reply(
            "да", state="pending", client_id=queued_cid
        )
        scr._ask_human_call_id = "q1"
        cid = scr._track_pending(display_text="да", wire_content="да")
        scr._track_answer(cid)
        sent = scr._mount_user_reply("да", state="pending", client_id=cid)
        feed_live(scr, [answer], splits=())
        await pilot.pause()
        assert sent.state == "normal"
        assert queued.state == "pending"
