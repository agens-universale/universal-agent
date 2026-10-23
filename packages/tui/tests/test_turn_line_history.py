"""Строка итога у каждого хода в ленте: оседание, очередь, откат, история.

Строка хода монтируется там, где в потоке записей треда стоит запись
`turn_outcome` (решение плана 7). Строка последнего хода свежая
(`-latest`): видна в ленте со сводкой и причиной до начала следующего
хода, тогда она оседает (S10 §3.2). Живая лента и переоткрытая дают один
порядок и один текст.
"""

from __future__ import annotations

import json
import threading
from datetime import timedelta

import pytest
from gc_core.execution.message_keys import withdraw_from_queue
from gc_core.message_policy.user_message_policy import (
    wrap_technical_user_content,
)
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_core.thread_store import (
    append_new_thread_messages,
    append_turn_outcome_record,
)
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.services.relative_time import parse_iso_utc

TID = "tline000000000000000000000000001a"
OTHER = "tline000000000000000000000000002b"
STARTED = "2026-10-22T11:30:00Z"
LATEST = "-latest"


def _ended(n: int) -> str:
    return f"2026-10-22T11:3{n}:12Z"


def _hm(iso: str) -> str:
    return parse_iso_utc(iso).astimezone().strftime("%H:%M")


def _done_line(n: int) -> str:
    return f"✓ Выполнил за 12с · {_hm(_ended(n))}"


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    """«Сейчас» — минута после конца последнего хода тестов: без даты."""
    from gc_tui.services import relative_time

    now = parse_iso_utc(_ended(9)).astimezone() + timedelta(minutes=1)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)


# --- тред на диске ----------------------------------------------------------


def _write_meta(tmp_path, tid=TID, tui=None):
    meta = {"thread_id": tid, "system_id": "cli"}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


def _write_thread(tmp_path, tid=TID, tui=None):
    _write_meta(tmp_path, tid, tui)
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _reply(mid, text=None):
    return {"id": mid, "role": "user", "content": text or f"вопрос {mid}"}


def _answer(mid, text=None):
    return {
        "id": mid,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text or f"ответ {mid}"}],
    }


def _outcome(n, opened_by, **kw):
    return TurnOutcome(
        status=kw.get("status", "done"),
        reason=kw.get("reason"),
        opened_by=opened_by,
        started_at=STARTED,
        ended_at=_ended(n),
        duration_sec=12.0,
        error=kw.get("error"),
    )


class _Thread:
    """Писатель файла треда, как ядро: сообщения и записи итога."""

    def __init__(self, threads_dir, tid=TID):
        self.dir = str(threads_dir)
        self.tid = tid
        self.seq = 0

    def messages(self, *payloads):
        append_new_thread_messages(
            self.dir,
            "cli",
            self.tid,
            list(payloads),
            set(),
            start_seq=self.seq,
        )
        self.seq += len(payloads)

    def record(self, n, opened_by, **kw):
        append_turn_outcome_record(
            self.dir,
            "cli",
            self.tid,
            _outcome(n, opened_by, **kw),
            seq=self.seq,
        )

    def turn(self, n, *, record=True):
        self.messages(_reply(f"u{n}"), _answer(f"a{n}"))
        if record:
            self.record(n, f"u{n}")


def _records(tmp_path, tid=TID):
    path = tmp_path / f"data_cli_{tid}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _outcome_openers(tmp_path, tid=TID):
    return [
        r["outcome"]["opened_by"]
        for r in _records(tmp_path, tid)
        if r.get("type") == "turn_outcome"
    ]


# --- экран ------------------------------------------------------------------


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _trace_lines(container) -> list[str]:
    """Видимый порядок ленты: реплики, ответы, строки ходов, сжатия."""
    from gc_tui.widgets.trace_row import CompactionRule, TraceRow
    from gc_tui.widgets.turn_line import TurnLine
    from gc_tui.widgets.user_reply import UserReplyContainer

    out = []
    for w in container.children:
        if isinstance(w, TurnLine):
            out.append(w.text.plain)
        elif isinstance(w, UserReplyContainer):
            out.append(f"› {w.plain_text}")
        elif isinstance(w, CompactionRule):
            out.append("сжатие")
        elif isinstance(w, TraceRow) and w.has_class("line-agent"):
            out.append("●")
    return out


def _turn_lines(scr):
    from gc_tui.widgets.turn_line import TurnLine

    return list(scr.query_one("#trace").query(TurnLine))


def _record_openers(scr) -> list:
    return [line.opened_by for line in _turn_lines(scr) if line.recorded]


def _fresh_line(scr):
    """Свежая строка последнего хода; индикатора итога нет."""
    from gc_tui.widgets.turn_line import TurnLine

    assert scr._thinking is None
    (line,) = scr.query_one("#trace").query(TurnLine).filter(f".{LATEST}")
    return line


async def _open(pilot, tid=TID):
    scr = pilot.app.screen
    scr.attach_active_task(tid, is_resume=True)
    await pilot.pause()
    return scr


async def _reopen(pilot, tmp_path, tid=TID):
    _write_thread(tmp_path, OTHER, tui={"status": "completed"})
    scr = pilot.app.screen
    scr.attach_active_task(OTHER, is_resume=True)
    scr.attach_active_task(tid, is_resume=True)
    await pilot.pause()
    return scr


async def _send_released(pilot, ex, text, n):
    """Отправка хода `n`, который ждёт, пока экран покажет реплику: иначе
    запись реплики может обогнать отправку."""
    scr = await _send(pilot, text)
    await wait_until(pilot, ex.held[n].is_set, f"ход {n} начался")
    ex.release[n].set()
    return scr


async def _send(pilot, text):
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.press("enter")
    await pilot.pause()
    return pilot.app.screen


# --- исполнитель: ход на каждый вызов, запись итога как у ядра --------------


class _TurnExecutor:
    """Ход — реплика, ответ и запись итога. Ходы из `holds` ждут своего
    `release[n]` (экран успевает открыть тред и показать реплику);
    `pickup` — ход подхватывает очередь сам (ядро закрыло ход и
    продолжило новым в том же прогоне); `tail` — очередь отдаётся
    раннеру хвостом, он доигрывает её отдельным прогоном."""

    def __init__(self, threads_dir, *, holds=(1,), pickup=False, tail=False):
        self.thread: _Thread | None = None
        self.dir = threads_dir
        self.n = 0
        self.pickup = pickup
        self.tail = tail
        self.queued: list[dict] = []
        self.held = {n: threading.Event() for n in holds}
        self.release = {n: threading.Event() for n in holds}

    def release_all(self):
        for event in self.release.values():
            event.set()

    async def arun(self, messages, *, thread_id, message_ids=None, **_kw):
        if self.thread is None:
            self.thread = _Thread(self.dir, thread_id)
            if not (self.dir / f"meta_cli_{thread_id}.json").exists():
                _write_meta(self.dir, thread_id)
        mid = (message_ids or [None])[0] or f"x{self.n + 1}"
        self._turn(mid, messages[0])
        while self.pickup and self.queued:
            nxt = self.queued.pop(0)
            self._turn(nxt["id"], nxt["content"])

    def _turn(self, mid, text):
        self.n += 1
        if self.n in self.release:
            self.held[self.n].set()
            self.release[self.n].wait(10)
        self.thread.messages(_reply(mid, text), _answer(f"a{self.n}"))
        self.thread.record(self.n, mid)

    def stop_run(self):
        self.release_all()

    def turn_effective_start(self):
        return None

    def add_message(self, message):
        self.queued.append(message)

    def withdraw_message(self, msg_id):
        return withdraw_from_queue(self.queued, msg_id)

    def take_undelivered_messages(self):
        if not self.tail:
            return []
        taken, self.queued = self.queued, []
        return taken


def _is_done(pilot, scr, n) -> bool:
    lines = [line for line in _turn_lines(scr) if line.is_fresh]
    return (
        not pilot.app.active_runner.is_running()
        and scr._thinking is None
        and len(lines) == 1
        and lines[0].text.plain == _done_line(n)
    )


async def _wait_done(pilot, scr, n):
    await wait_until(
        pilot, lambda: _is_done(pilot, scr, n), f"итог хода {n} в ленте"
    )


# --- живая лента: оседание и равенство с переоткрытием ----------------------


@pytest.mark.asyncio
async def test_previous_turn_settles_when_next_starts_and_matches_reopen(
    tmp_path,
):
    ex = _TurnExecutor(tmp_path, holds=(1, 2))
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        try:
            scr = await _send(pilot, "первый")
            ex.release[1].set()
            await _wait_done(pilot, scr, 1)
            (line,) = _turn_lines(scr)
            assert line.has_class(LATEST)
            assert line.opened_by is not None

            scr = await _send(pilot, "второй")
            await wait_until(pilot, ex.held[2].is_set, "второй ход начался")
            await pilot.pause()
            trace = scr.query_one("#trace")
            assert _trace_lines(trace) == [
                "› первый",
                "●",
                _done_line(1),
                "› второй",
            ]
            await wait_until(
                pilot,
                lambda: (
                    scr._thinking is not None
                    and scr._thinking.state == "thinking"
                ),
                "индикатор — у второго хода",
            )
        finally:
            ex.release_all()
        await _wait_done(pilot, scr, 2)
        live = _trace_lines(scr.query_one("#trace"))
        assert live == [
            "› первый",
            "●",
            _done_line(1),
            "› второй",
            "●",
            _done_line(2),
        ]

        scr = await _reopen(pilot, tmp_path, scr._active_task_id)
        assert _trace_lines(scr.query_one("#trace")) == live
        assert _fresh_line(scr).text.plain == _done_line(2)


@pytest.mark.asyncio
async def test_queue_pickup_in_one_run_settles_line_above_next_reply(
    tmp_path,
):
    """Реплика во время хода, ядро закрыло ход и подхватило её тем же
    прогоном: запись первого хода приходит хвостом посреди прогона —
    строка над репликой второго, видимая; сводка правок второго хода
    считается от новой границы журнала."""
    ex = _TurnExecutor(tmp_path, pickup=True)
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        try:
            scr = await _send(pilot, "первый")
            await wait_until(pilot, ex.held[1].is_set, "первый ход идёт")
            scr = await _send(pilot, "второй")
            await wait_until(pilot, lambda: ex.queued, "реплика в очереди")
        finally:
            ex.release_all()
        await _wait_done(pilot, scr, 2)
        trace = scr.query_one("#trace")
        assert _trace_lines(trace) == [
            "› первый",
            "●",
            _done_line(1),
            "› второй",
            "●",
            _done_line(2),
        ]
        first, second = _turn_lines(scr)
        assert not first.has_class(LATEST)
        assert second.has_class(LATEST)


@pytest.mark.asyncio
async def test_queue_baseline_recaptured_when_core_picks_up(tmp_path):
    """Подхват очереди ядром — новая граница журнала правок: сводка хода
    из очереди не тянет правки первого."""
    _write_thread(tmp_path)
    thread = _Thread(tmp_path)
    thread.turn(1)
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = await _open(pilot)
        turns = iter(["t-before", "t-after"])
        lookup = type("L", (), {"last_turn_id": lambda _s: next(turns)})
        scr._edits_lookup_for_thread = lambda: lookup()

        class _Running:
            _thread_kind = "run"
            _last_target = (TID, "cli")
            run_seq = 1

            def is_running(self):
                return True

            def turn_effective_start(self):
                return None

        pilot.app.active_runner = _Running()
        scr._prepare_for_new_run()
        scr._poll_active()  # восходящий фронт прогона уже позади
        assert scr._edits_baseline_turn_id == "t-before"
        thread.messages(_reply("u2", "второй"), _answer("a2"))
        thread.record(2, "u2")
        scr._poll_trace_tail()
        await pilot.pause()
        assert _turn_lines(scr)[-1].has_class(LATEST)
        thread.messages(_reply("u3", "третий"))
        scr._poll_trace_tail()
        await pilot.pause()
        assert not _turn_lines(scr)[-1].has_class(LATEST)
        assert scr._edits_baseline_turn_id == "t-after"
        pilot.app.active_runner = None


@pytest.mark.asyncio
async def test_queued_reply_after_final_is_own_turn_with_own_line(tmp_path):
    """Реплика опоздала к подхвату: раннер доигрывает её отдельным ходом
    (хвост) — у каждого хода своя строка, первая осела над репликой."""
    ex = _TurnExecutor(tmp_path, tail=True)
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        try:
            scr = await _send(pilot, "первый")
            await wait_until(pilot, ex.held[1].is_set, "первый ход идёт")
            scr = await _send(pilot, "второй")
            await wait_until(pilot, lambda: ex.queued, "реплика в очереди")
        finally:
            ex.release_all()
        await _wait_done(pilot, scr, 2)
        assert _trace_lines(scr.query_one("#trace")) == [
            "› первый",
            "●",
            _done_line(1),
            "› второй",
            "●",
            _done_line(2),
        ]
        assert [line.has_class(LATEST) for line in _turn_lines(scr)] == [
            False,
            True,
        ]


# --- откат по Esc -----------------------------------------------------------
#
# Строки по записям итога. Запасная строка, которую спадающий фронт
# откатанного прогона ещё может поставить, — забота гарда отката (S10 §3.8).


class _FakeRunner:
    _thread_kind = "run"

    def __init__(self):
        self._running = True
        self._last_target = (TID, "cli")
        self.run_seq = 1

    def is_running(self):
        return self._running

    def stop(self):
        """Поток прогона досиживает после стопа: бежит, пока тест не
        отпустит (`_running = False`)."""

    def cancel_ask_human(self):
        pass

    def turn_effective_start(self):
        return None


async def _undo_setup(pilot, tmp_path):
    """Тред с закрытым ходом 1 на экране, отправлена реплика `m_orphan`
    (на диске), раннер бежит."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    scr = await _open(pilot)
    pilot.app.tui_state.active_task_id = TID
    runner = _FakeRunner()
    pilot.app.active_runner = runner
    scr._prepare_for_new_run()
    thread = _Thread(tmp_path)
    thread.seq = 2
    thread.messages(_reply("m_orphan", "отменяю"))
    scr._last_user_message_id = "m_orphan"
    scr._trace_mount(UserReplyContainer("отменяю", event_no=0))
    await pilot.pause()
    return scr, thread, runner


def _prepare_one_turn(tmp_path):
    _write_thread(tmp_path)
    _Thread(tmp_path).turn(1)


@pytest.mark.asyncio
async def test_undo_with_record_before_discard_leaves_no_line(tmp_path):
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, _runner = await _undo_setup(pilot, tmp_path)
        thread.record(2, "m_orphan", status="stopped", reason="user_stop")

        scr._cancel_with_undo()
        await pilot.pause()
        scr._poll_trace_tail()
        await pilot.pause()

        assert _outcome_openers(tmp_path) == ["u1"]
        assert _record_openers(scr) == ["u1"]
        # Прошлый ход снова последний (S10 §3.8, В2).
        assert _turn_lines(scr)[0].has_class(LATEST)


@pytest.mark.asyncio
async def test_undo_with_record_after_discard_backstop_removes_line(
    tmp_path,
):
    """Поток старого прогона пишет запись уже после немедленного отката:
    тик дочитывает её, но строки снятой реплики нет; backstop снимает
    запись из файла."""
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, runner = await _undo_setup(pilot, tmp_path)
        scr._cancel_with_undo()
        await pilot.pause()
        assert scr._pending_orphan_cleanups

        thread.record(2, "m_orphan", status="stopped", reason="user_stop")
        scr._poll_trace_tail()
        await pilot.pause()
        assert _record_openers(scr) == ["u1"]
        assert scr._last_turn_outcome.opened_by == "u1"

        runner._running = False
        scr._run_orphan_cleanups()
        await pilot.pause()

        assert not scr._pending_orphan_cleanups
        assert _outcome_openers(tmp_path) == ["u1"]
        assert _record_openers(scr) == ["u1"]
        assert scr._last_turn_outcome.opened_by == "u1"
        tail = scr._store.tail_events(TID, scr._cursor)
        assert tail.stream_reset is False


@pytest.mark.asyncio
async def test_zz_late_stop_record_of_undone_reply_never_shows_a_line(
    tmp_path,
):
    """Откат отправки, поток старого прогона висит в HTTP-повторах и пишет
    запись стопа позже, когда уже идёт новый ход того же треда. Строки
    «■ Отменено» для снятой реплики на экране быть не должно."""
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, old = await _undo_setup(pilot, tmp_path)
        scr._cancel_with_undo()
        await pilot.pause()
        new = _FakeRunner()  # повторная отправка: новый ход того же треда
        pilot.app.active_runner = new
        thread.record(2, "m_orphan", status="stopped", reason="user_stop")
        old._running = False  # старый поток дописал итог и вышел
        for _ in range(3):
            scr._poll_tick()
            await pilot.pause()
        assert _record_openers(scr) == ["u1"]


@pytest.mark.asyncio
async def test_late_stop_record_after_leaving_and_returning_shows_no_line(
    tmp_path,
):
    """Откат, уход в другой тред и возврат, пока остановленный поток висит
    в повторах: его поздняя запись стопа строки не даёт. Набор снятых
    реплик треда живёт до его чистки."""
    _prepare_one_turn(tmp_path)
    _write_thread(tmp_path, OTHER, tui={"status": "completed"})
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, old = await _undo_setup(pilot, tmp_path)
        scr._cancel_with_undo()
        await pilot.pause()

        await _open(pilot, OTHER)
        await _open(pilot, TID)
        thread.record(2, "m_orphan", status="stopped", reason="user_stop")
        scr._poll_trace_tail()
        await pilot.pause()
        # Строки по записям; строка по статусу сессии после отката — до
        # возврата меты откатом (S10 §3.8).
        assert _record_openers(scr) == ["u1"]

        old._running = False
        scr._run_orphan_cleanups()
        await pilot.pause()
        assert TID not in scr._undone_reply_ids
        assert _record_openers(scr) == ["u1"]


@pytest.mark.asyncio
async def test_undo_after_record_was_tailed_removes_mounted_line(tmp_path):
    """Запись дочитана хвостом и смонтирована до отката (курсор за ней):
    откат снимает строку по `opened_by` и пересинхронизирует курсор —
    без перестройки ленты."""
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, _runner = await _undo_setup(pilot, tmp_path)
        thread.record(2, "m_orphan", status="stopped", reason="user_stop")
        scr._poll_trace_tail()
        await pilot.pause()
        assert _turn_lines(scr)[-1].opened_by == "m_orphan"
        rebuilt: list = []
        scr._rebuild_from_store = lambda: rebuilt.append(True)

        scr._cancel_with_undo()
        await pilot.pause()

        assert _outcome_openers(tmp_path) == ["u1"]
        assert _record_openers(scr) == ["u1"]
        tail = scr._store.tail_events(TID, scr._cursor)
        assert tail.stream_reset is False
        scr._poll_trace_tail()
        assert rebuilt == []


# --- S10 §3.8 (В2): откат до вывода возвращает свежесть ---------------------

_PRE_TUI = {
    "status": "completed",
    "last_indicator": {"state": "done", "edits": [2, 14, 3]},
}


def _tui_meta(tmp_path, tid=TID) -> dict:
    meta = json.loads((tmp_path / f"meta_cli_{tid}.json").read_text())
    return meta.get("tui") or {}


def _prepare_one_turn_with_edits(tmp_path):
    _write_thread(tmp_path, tui=dict(_PRE_TUI))
    _Thread(tmp_path).turn(1)


def _line_view(scr):
    return [(w.text.plain, w.is_fresh, w.summary) for w in _turn_lines(scr)]


@pytest.mark.asyncio
async def test_undo_restores_previous_line_freshness(tmp_path):
    """Откат отправки: прошлая строка снова свежая с прежней сводкой, мета
    — как до отправки, бара нет."""
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        before = _fresh_line(scr).summary
        assert before == (2, 14, 3)
        scr, _thread, _runner = await _undo_setup(pilot, tmp_path)
        assert not _turn_lines(scr)[0].is_fresh
        assert _tui_meta(tmp_path)["status"] == "running"

        scr._cancel_with_undo()
        await pilot.pause()

        line = _fresh_line(scr)
        assert line.opened_by == "u1"
        assert line.summary == before
        tui = _tui_meta(tmp_path)
        assert tui["status"] == _PRE_TUI["status"]
        assert tui["last_indicator"] == _PRE_TUI["last_indicator"]
        assert not scr.query_one("#indicator-bar").has_class("-turn")


@pytest.mark.asyncio
async def test_undo_on_first_turn_restores_meta_snapshot(tmp_path):
    """Первый ход откатан: строки нет, мета без «running» — как до
    отправки."""
    _write_thread(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        from gc_tui.widgets.user_reply import UserReplyContainer

        scr = await _open(pilot)
        pilot.app.tui_state.active_task_id = TID
        pilot.app.active_runner = _FakeRunner()
        scr._prepare_for_new_run()
        _Thread(tmp_path).messages(_reply("m_first", "первый"))
        scr._last_user_message_id = "m_first"
        scr._trace_mount(UserReplyContainer("первый", event_no=0))
        await pilot.pause()

        scr._cancel_with_undo()
        await pilot.pause()

        assert _turn_lines(scr) == []
        tui = _tui_meta(tmp_path)
        assert tui.get("status") is None
        assert tui.get("last_indicator") is None
        assert not scr.query_one("#indicator-bar").has_class("-turn")


@pytest.mark.asyncio
async def test_undo_run_never_finishes(tmp_path):
    """Поток откатанного прогона выходит позже: исход его прогона не
    ставится и в мету не пишется, бар не возвращается."""
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, _thread, runner = await _undo_setup(pilot, tmp_path)
        scr._poll_tick()  # тик застал прогон
        finished = []
        scr._on_run_finished = lambda r, k: finished.append(k)
        scr._cancel_with_undo()
        await pilot.pause()
        runner._running = False
        for _ in range(3):
            scr._poll_tick()
            await pilot.pause()

        assert finished == []
        assert _tui_meta(tmp_path)["status"] == _PRE_TUI["status"]
        assert _record_openers(scr) == ["u1"]
        assert [w.opened_by for w in _turn_lines(scr)] == ["u1"]
        assert _fresh_line(scr).opened_by == "u1"
        assert not scr.query_one("#indicator-bar").has_class("-turn")


@pytest.mark.asyncio
async def test_undo_reopen_equals_live(tmp_path):
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, _thread, runner = await _undo_setup(pilot, tmp_path)
        scr._cancel_with_undo()
        await pilot.pause()
        live = _line_view(scr)
        runner._running = False
        scr._run_orphan_cleanups()
        scr = await _reopen(pilot, tmp_path)
        assert _line_view(scr) == live
        assert live[0][1] is True


@pytest.mark.asyncio
async def test_undo_after_held_record_freshens_previous_line(tmp_path):
    """Запись откатанного хода уже дочитана и стоит за баром: откат в том
    же обработчике снимает её строку и освежает строку прошлого хода, а не
    снимаемую. Живой вид — как после переоткрытия."""
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, thread, runner = await _undo_setup(pilot, tmp_path)
        thread.record(2, "m_orphan", status="stopped", reason="user_stop")
        scr._poll_trace_tail()
        assert _turn_lines(scr)[-1].has_class("-held")

        scr._cancel_with_undo()  # без паузы: снятая строка ещё в query
        await pilot.pause()

        line = _fresh_line(scr)
        assert line.opened_by == "u1"
        assert line.summary == (2, 14, 3)
        live = _line_view(scr)
        runner._running = False
        scr._run_orphan_cleanups()
        scr = await _reopen(pilot, tmp_path)
        assert _line_view(scr) == live


@pytest.mark.asyncio
async def test_no_undo_for_settled_run(tmp_path):
    """Ошибка закрыла прогон при живом потоке (строка «✕» стоит): отката
    у зафиксированного прогона нет — Esc его не откатывает."""
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, _thread, runner = await _undo_setup(pilot, tmp_path)
        runner.produced_output = lambda: False
        scr._poll_tick()
        pilot.app.last_runner_error = "ReadTimeout: сервер не ответил"
        scr._poll_tick()
        await pilot.pause()
        assert _turn_lines(scr)[-1].word == "failed"
        assert scr._can_cancel_with_undo() is False


# --- история треда ----------------------------------------------------------


def _summary_text(text):
    return (
        '<technical type="dialog_summary">'
        f"<summary>{text}</summary></technical>"
    )


def _write_rows(path, rows):
    with open(path, "w", encoding="utf-8") as f:
        for row in rows:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")


def _msg_row(seq, payload):
    return {
        "type": "message",
        "thread_id": TID,
        "system": "cli",
        "seq": seq,
        "ts": "2026-10-22T11:30:00Z",
        "payload": payload,
    }


def _outcome_row(seq, n, opened_by):
    return {
        "type": "turn_outcome",
        "thread_id": TID,
        "system": "cli",
        "seq": seq,
        "ts": "2026-10-22T11:30:00Z",
        "outcome": _outcome(n, opened_by).to_payload(),
    }


def write_compacted_thread(tmp_path):
    """Сжатие конца хода 2: ходы 1–2 с записями — в архиве, в живом
    файле сводка и ход 3."""
    _write_meta(tmp_path, tui={"status": "completed"})
    _write_rows(
        tmp_path / f"data_cli_{TID}__pre_summary__20261022_113300.jsonl",
        [
            _msg_row(1, _reply("u1")),
            _msg_row(2, _answer("a1")),
            _outcome_row(2, 1, "u1"),
            _msg_row(3, _reply("u2")),
            _msg_row(4, _answer("a2")),
            _outcome_row(4, 2, "u2"),
        ],
    )
    _write_rows(
        tmp_path / f"data_cli_{TID}.jsonl",
        [
            _msg_row(
                1,
                {
                    "id": "s1",
                    "role": "user",
                    "content": _summary_text("сводка"),
                },
            ),
            _msg_row(2, _reply("u3")),
            _msg_row(3, _answer("a3")),
            _outcome_row(3, 3, "u3"),
        ],
    )


COMPACTED_TRACE = [
    "› вопрос u1",
    "●",
    _done_line(1),
    "› вопрос u2",
    "●",
    _done_line(2),
    "сжатие",
    "› вопрос u3",
    "●",
    _done_line(3),
]


@pytest.mark.asyncio
async def test_turn_end_compaction_keeps_archived_turn_lines(tmp_path):
    write_compacted_thread(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        assert _trace_lines(scr.query_one("#trace")) == COMPACTED_TRACE
        assert _fresh_line(scr).text.plain == _done_line(3)


@pytest.mark.asyncio
async def test_last_archived_turn_is_indicator_until_next_turn(tmp_path):
    """Сжатие конца последнего хода: его запись — в архиве, за ней только
    сводка — строка последнего хода остаётся свежей над маркером."""
    _write_meta(tmp_path, tui={"status": "completed"})
    _write_rows(
        tmp_path / f"data_cli_{TID}__pre_summary__20261022_113300.jsonl",
        [
            _msg_row(1, _reply("u1")),
            _msg_row(2, _answer("a1")),
            _outcome_row(2, 1, "u1"),
        ],
    )
    _write_rows(
        tmp_path / f"data_cli_{TID}.jsonl",
        [
            _msg_row(
                1,
                {
                    "id": "s1",
                    "role": "user",
                    "content": _summary_text("сводка"),
                },
            ),
        ],
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        (line,) = _turn_lines(scr)
        assert line is _fresh_line(scr)
        assert line.text.plain == _done_line(1)


@pytest.mark.asyncio
async def test_foreign_turn_has_line_and_turn_without_record_has_none(
    tmp_path,
):
    """Ход другого фронтенда (запись есть) — своя строка; ход без записи
    (старый CLI, убитый процесс) посреди треда — строки нет."""
    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.turn(2)  # другой фронтенд: запись пишет ядро
    thread.turn(3, record=False)
    thread.turn(4)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        assert _trace_lines(scr.query_one("#trace")) == [
            "› вопрос u1",
            "●",
            _done_line(1),
            "› вопрос u2",
            "●",
            _done_line(2),
            "› вопрос u3",
            "●",
            "› вопрос u4",
            "●",
            _done_line(4),
        ]
        assert [line.opened_by for line in _turn_lines(scr)] == [
            "u1",
            "u2",
            "u4",
        ]


@pytest.mark.asyncio
async def test_failed_earlier_turn_is_one_row_without_reason(tmp_path):
    """Решение владельца 4: у прошлого упавшего хода — одна строка без
    причины; причина — только у свежей строки последнего хода."""
    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    thread.messages(_reply("u1"), _answer("a1"))
    thread.record(
        1,
        "u1",
        status="error",
        error={"type": "ReadError", "message": "ReadError: boom"},
    )
    thread.turn(2)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        first = _turn_lines(scr)[0]
        assert first.text.plain == f"✕ Ошибка · {_hm(_ended(1))}"
        assert "boom" not in _trace_lines(scr.query_one("#trace"))[2]


@pytest.mark.asyncio
async def test_interrupted_line_at_trace_end_on_reopen(tmp_path):
    """Континуация плана оборвана без записи: «■ Прервано» без времени —
    свежая строка в конце ленты (§3.6), строка хода с планом осевшая."""
    _write_thread(tmp_path, tui={"status": "running"})
    thread = _Thread(tmp_path)
    thread.messages(_reply("u1"), _answer("a1"))
    thread.record(1, "u1", reason="plan_proposed")
    thread.messages(
        {
            "id": "t1",
            "role": "user",
            "content": wrap_technical_user_content(
                "план утверждён", technical_type="technical"
            ),
        }
    )
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        plan_line, interrupted = _turn_lines(scr)
        assert scr.query_one("#trace").children[-1] is interrupted
        assert interrupted is _fresh_line(scr)
        assert interrupted.text.plain == "■ Прервано"
        assert interrupted.recorded is False
        assert not plan_line.is_fresh
        assert plan_line.text.plain == (
            f"✓ План готов за 12с · {_hm(_ended(1))}"
        )


@pytest.mark.asyncio
async def test_next_day_redraw_says_yesterday(tmp_path, monkeypatch):
    from gc_tui.services import relative_time

    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.turn(2)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        assert _turn_lines(scr)[0].text.plain == _done_line(1)
        tomorrow = parse_iso_utc(_ended(1)).astimezone() + timedelta(days=1)
        monkeypatch.setattr(relative_time, "_now_local", lambda: tomorrow)
        scr = await _reopen(pilot, tmp_path)
        assert _turn_lines(scr)[0].text.plain == (
            f"✓ Выполнил за 12с · вчера {_hm(_ended(1))}"
        )


@pytest.mark.asyncio
async def test_fast_failed_run_reply_delivered_in_same_tick_is_shown_once(
    tmp_path,
):
    """Ход упал между тиками, реплика уже в файле. Один тик видит и
    ошибку раннера, и запись реплики: реплика на экране одна, в мете нет
    «failed»-остатка."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        pilot.app.tui_state.active_task_id = TID
        runner = _FakeRunner()
        runner._running = False
        pilot.app.active_runner = runner
        cid = scr._track_pending(display_text="вопрос", wire_content="вопрос")
        scr._mount_user_reply("вопрос", state="pending", client_id=cid)
        await pilot.pause()
        thread = _Thread(tmp_path)
        thread.seq = 2
        thread.messages(_reply("m2", "вопрос"))
        pilot.app.last_runner_error = "MainModelUnavailable: 404"
        scr._poll_tick()
        await pilot.pause()
        scr._poll_tick()
        await pilot.pause()
        replies = [
            w
            for w in scr.query(UserReplyContainer)
            if w.plain_text == "вопрос"
        ]
        meta = json.loads((tmp_path / f"meta_cli_{TID}.json").read_text())
        pending = (meta.get("tui") or {}).get("pending_user_messages", [])
        assert len(replies) == 1
        assert pending == []


# --- S10: итог последнего хода живёт в ленте --------------------------------


def _compact_files(tmp_path, suffix="20261022_113500"):
    """Сжатие на диске, как у ядра: живой файл — в архив, новый — со
    сводкой, мета с эпохой сжатия."""
    from gc_core.thread_store import (
        archive_and_replace_thread_files,
        load_thread_meta,
        save_thread_meta,
    )

    meta = load_thread_meta(str(tmp_path), "cli", TID)
    archive_and_replace_thread_files(
        str(tmp_path),
        "cli",
        TID,
        [{"id": f"s{suffix}", "role": "user", "content": _summary_text("c")}],
        archive_suffix=suffix,
    )
    save_thread_meta(
        str(tmp_path),
        "cli",
        TID,
        {**meta, "last_summary_archive_suffix": suffix},
    )


def _child_types(scr) -> list[str]:
    return [type(w).__name__ for w in scr.query_one("#trace").children]


@pytest.mark.asyncio
async def test_latest_turn_line_visible_in_trace(tmp_path):
    ex = _TurnExecutor(tmp_path)
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        scr = await _send_released(pilot, ex, "первый", 1)
        await _wait_done(pilot, scr, 1)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.is_fresh
        assert line.display
        assert line.region.height == 1
        assert scr._thinking is None


@pytest.mark.asyncio
async def test_compact_marker_lands_below_fresh_turn_line(tmp_path):
    from gc_tui.widgets.trace_row import CompactionRule
    from gc_tui.widgets.turn_line import TurnLine

    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        _compact_files(tmp_path)
        scr._poll_trace_tail()
        await pilot.pause()
        trace = scr.query_one("#trace")
        kids = list(trace.children)
        (line,) = [w for w in kids if isinstance(w, TurnLine)]
        (rule,) = [w for w in kids if isinstance(w, CompactionRule)]
        assert kids.index(line) < kids.index(rule)
        assert line is _fresh_line(scr)


@pytest.mark.asyncio
async def test_bang_output_keeps_line_fresh(tmp_path):
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        line = _fresh_line(scr)
        scr._handle_bang_shell("!")
        await pilot.pause()
        trace = scr.query_one("#trace")
        assert trace.children[-1] is not line
        assert list(trace.children).index(line) < len(trace.children) - 1
        assert line.is_fresh


@pytest.mark.asyncio
async def test_next_turn_settles_line(tmp_path):
    from gc_tui.widgets.user_reply import UserReplyContainer

    ex = _TurnExecutor(tmp_path, holds=(1, 2))
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        try:
            scr = await _send_released(pilot, ex, "первый", 1)
            await _wait_done(pilot, scr, 1)
            (line,) = _turn_lines(scr)
            scr = await _send(pilot, "второй")
            await wait_until(pilot, ex.held[2].is_set, "второй ход идёт")
            await pilot.pause()
            assert not line.is_fresh
            line.render()
            assert line.summary_span is None
            assert line.region.height == 1
            kids = list(scr.query_one("#trace").children)
            (reply,) = [
                w
                for w in kids
                if isinstance(w, UserReplyContainer)
                and w.plain_text == "второй"
            ]
            assert kids.index(line) < kids.index(reply)
        finally:
            ex.release_all()
        await _wait_done(pilot, scr, 2)


@pytest.mark.asyncio
async def test_queued_reply_keeps_line_fresh(tmp_path):
    """Запись хода дочитана, под ней реплика в очереди: строка свежая и
    стоит над очередью (§4.3)."""
    _write_thread(tmp_path)
    thread = _Thread(tmp_path)
    thread.messages(_reply("u1"))
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        queued = scr._mount_user_reply("в очередь", state="queued")
        await pilot.pause()
        thread.messages(_answer("a1"))
        thread.record(1, "u1")
        scr._poll_trace_tail()
        await pilot.pause()
        line = _turn_lines(scr)[-1]
        assert line.is_fresh
        kids = list(scr.query_one("#trace").children)
        assert kids.index(line) < kids.index(queued)


@pytest.mark.asyncio
async def test_pending_reply_below_makes_record_line_settled(tmp_path):
    """Запись стопа дочитана после новой отправки: строка встаёт над
    отправленной репликой уже осевшей — следующий ход начался."""
    _write_thread(tmp_path, tui={"status": "running"})
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.messages(_reply("u2"))
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        cid = scr._track_pending(display_text="ещё", wire_content="ещё")
        sent = scr._mount_user_reply("ещё", state="pending", client_id=cid)
        await pilot.pause()
        thread.record(2, "u2", status="stopped", reason="user_stop")
        scr._poll_trace_tail()
        await pilot.pause()
        line = _turn_lines(scr)[-1]
        assert line.opened_by == "u2"
        assert not line.is_fresh
        kids = list(scr.query_one("#trace").children)
        assert kids.index(line) < kids.index(sent)


@pytest.mark.asyncio
async def test_new_record_resets_superseded(tmp_path):
    """Реплика после записи — ход идёт (статуса нет: строки по статусу
    тоже); новая запись снова даёт свежую строку."""
    _write_thread(tmp_path)
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.messages(_reply("u2"))
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        assert scr._outcome_superseded is True
        thread.messages(_answer("a2"))
        thread.record(2, "u2")
        scr._poll_trace_tail()
        await pilot.pause()
        assert scr._outcome_superseded is False
        first, second = _turn_lines(scr)
        assert not first.is_fresh
        assert second.is_fresh


@pytest.mark.asyncio
async def test_live_and_reopen_turn_line_order_equal(tmp_path):
    """Ход, сжатие, ход: типы детей ленты вживую — как после
    переоткрытия."""
    ex = _TurnExecutor(tmp_path, holds=(1, 2))
    app = _app(tmp_path)
    app._executor_factory = lambda: ex
    async with app.run_test() as pilot:
        scr = await _send_released(pilot, ex, "первый", 1)
        await _wait_done(pilot, scr, 1)
        global TID
        tid, TID = TID, scr._active_task_id
        try:
            _compact_files(tmp_path)
        finally:
            TID = tid
        ex.thread.seq = 1
        scr._poll_trace_tail()
        await pilot.pause()
        scr = await _send_released(pilot, ex, "второй", 2)
        await _wait_done(pilot, scr, 2)
        live = _child_types(scr)
        assert live.count("TurnLine") == 2
        assert "CompactionRule" in live
        scr = await _reopen(pilot, tmp_path, scr._active_task_id)
        assert _child_types(scr) == live


# --- S10: раскладка покоя — бара нет, зазор ленты одна строка --------------


@pytest.mark.asyncio
@pytest.mark.parametrize("failed", [False, True])
async def test_idle_gap_is_one_row(tmp_path, failed):
    """Между низом строки итога (с `└` — низом последней `└`) и рамкой поля
    ввода — ровно одна пустая строка: нижний зазор ленты, бара нет."""
    _write_thread(
        tmp_path, tui={"status": "failed" if failed else "completed"}
    )
    thread = _Thread(tmp_path)
    thread.messages(_reply("u1"), _answer("a1"))
    error = {"type": "ReadError", "message": "ReadError: boom"}
    if failed:
        thread.record(1, "u1", status="error", error=error)
    else:
        thread.record(1, "u1")
    async with _app(tmp_path).run_test(size=(80, 30)) as pilot:
        scr = await _open(pilot)
        await pilot.pause()
        line = _fresh_line(scr)
        assert line.region.height == (2 if failed else 1)
        input_row = scr.query_one("#input-row")
        assert input_row.region.y - line.region.bottom == 1


@pytest.mark.asyncio
async def test_line_held_while_bar_up(tmp_path):
    """Запись итога дочитана раньше спадающего фронта: строка ждёт за
    баром (`-held`); бар уходит — строка встаёт свежей в том же вызове."""
    from gc_tui.widgets.turn_line import TurnLine

    _write_thread(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        scr._prepare_for_new_run()
        thread = _Thread(tmp_path)
        thread.messages(_reply("u1"), _answer("a1"))
        thread.record(1, "u1")
        scr._poll_trace_tail()
        (line,) = scr.query_one("#trace").query(TurnLine)
        assert line.has_class("-held")
        assert line.is_fresh
        bar = scr.query_one("#indicator-bar")
        assert bar.has_class("-turn")

        runner._running = False
        scr._settle_ended_run()
        assert not bar.has_class("-turn")
        assert not line.has_class("-held")
        assert line.is_fresh
        await pilot.pause()
        assert line.display


class _CompactRunner(_FakeRunner):
    """Идёт `/compact`: прогон сжатия открытого треда."""

    _thread_kind = "compact"


def _held(widget) -> bool:
    return widget.has_class("-held")


@pytest.mark.asyncio
async def test_compact_marker_held_until_bar_leaves(tmp_path):
    """`/compact` после хода: маркер сжатия встаёт под свежей строкой и
    ждёт за баром «сжимаю контекст…»; бар уходит — маркер раскрыт в том
    же вызове, строка всё время свежая и видна."""
    from gc_tui.widgets.trace_row import CompactionRule

    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        line = _fresh_line(scr)
        runner = _CompactRunner()
        pilot.app.active_runner = runner
        scr._run_pending = True  # принятый /compact
        scr._sync_indicator_bar()
        _compact_files(tmp_path)
        scr._poll_trace_tail()
        (rule,) = scr.query_one("#trace").query(CompactionRule)
        assert _held(rule)
        assert not _held(line)
        assert line.is_fresh

        runner._running = False
        scr._settle_ended_run()
        assert not scr.query_one("#indicator-bar").has_class("-turn")
        assert not _held(rule)
        assert line.is_fresh


@pytest.mark.asyncio
async def test_autocompaction_marker_mid_run_not_held(tmp_path):
    """Сжатие посреди хода (прогон хода, не `/compact`): маркер не ждёт
    бара — бар после него остаётся."""
    from gc_tui.widgets.trace_row import CompactionRule

    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        pilot.app.active_runner = _FakeRunner()
        scr._prepare_for_new_run()
        _compact_files(tmp_path)
        scr._poll_trace_tail()
        (rule,) = scr.query_one("#trace").query(CompactionRule)
        assert not _held(rule)


@pytest.mark.asyncio
async def test_rebuild_with_live_runner_holds_last_line(tmp_path):
    """Перерисовка (`stream_reset`) при живом раннере: строки ходов — на
    местах записей; последняя ждёт за баром, прошлые — осевшие и видны."""
    _write_thread(tmp_path)
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.turn(2)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        scr._prepare_for_new_run()
        scr._rebuild_from_store()
        await pilot.pause()  # старые виджеты ленты сняты
        first, last = _turn_lines(scr)
        assert not _held(first)
        assert not first.is_fresh
        assert _held(last)

        runner._running = False
        scr._settle_ended_run()
        assert not _held(last)


@pytest.mark.asyncio
async def test_bar_removal_deferred_under_select_freeze(tmp_path):
    """Ход кончился во время выделения (F2): бар и придержанная строка
    не меняются, мета пишется сразу; выделение снято — бар уходит и
    строка встаёт в одном тике."""
    _write_thread(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        scr._prepare_for_new_run()
        thread = _Thread(tmp_path)
        thread.messages(_reply("u1"), _answer("a1"))
        thread.record(1, "u1")
        scr._poll_trace_tail()
        (line,) = _turn_lines(scr)
        bar = scr.query_one("#indicator-bar")

        frozen = [True]
        scr._ui_frozen_for_select = lambda: frozen[0]
        runner._running = False
        scr._poll_tick()
        assert bar.has_class("-turn")
        assert _held(line)
        tui = scr._store.get_task_meta(TID)["tui"]
        assert tui["status"] == "completed"

        frozen[0] = False
        scr._poll_tick()
        assert not bar.has_class("-turn")
        assert not _held(line)
        assert line.is_fresh


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("fallback_by", "record_by"),
    [("u2", "u2"), (None, "u2"), ("u2", None)],
)
async def test_runner_fallback_line_replaced_by_late_record(
    tmp_path, fallback_by, record_by
):
    """Запасная строка хода без записи (спадающий фронт раньше записи):
    запись, дочитанная позже, заменяет её — один ход, одна строка. Пустой
    `opened_by` у одной из двух — заменяется самая новая строка без
    записи; свежесть сохраняется."""
    _prepare_one_turn(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        thread = _Thread(tmp_path)
        thread.seq = 2
        thread.messages(_reply("u2"), _answer("a2"))
        scr._poll_trace_tail()
        scr._last_user_message_id = fallback_by
        scr._show_turn_outcome("completed", False, None)
        assert [line.recorded for line in _turn_lines(scr)] == [True, False]

        thread.record(2, record_by)
        scr._poll_trace_tail()
        await pilot.pause()
        first, second = _turn_lines(scr)
        assert second.recorded
        assert second.text.plain == _done_line(2)
        assert second is _fresh_line(scr)
        assert not first.is_fresh


@pytest.mark.asyncio
async def test_quit_mid_run_reopen_shows_line_at_end(tmp_path):
    """`/quit` посреди хода, запись стопа не успела: при переоткрытии —
    «■ Отменено» без времени по статусу `stopped`, в конце ленты."""
    _write_thread(tmp_path, tui={"status": "stopped"})
    thread = _Thread(tmp_path)
    thread.turn(1)
    thread.messages(_reply("u2"), _answer("a2"))
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        first, last = _turn_lines(scr)
        assert scr.query_one("#trace").children[-1] is last
        assert last is _fresh_line(scr)
        assert last.text.plain == "■ Отменено"
        assert last.recorded is False
        assert not first.is_fresh


# --- S10 §3.5: рамка плана над свежей строкой ------------------------------


def _write_plan_thread(tmp_path, text):
    """Тред TID, в мете — предложенный план сессии."""
    from gc_core.execution.state import SharedStateData
    from gc_core.persistence.schema import serialize_shared_block
    from gc_core.plan_store import new_plan_block

    _write_thread(tmp_path)
    shared = SharedStateData(plan=new_plan_block(text), permission_mode="plan")
    meta = json.loads((tmp_path / f"meta_cli_{TID}.json").read_text())
    meta["shared"] = serialize_shared_block(shared)
    (tmp_path / f"meta_cli_{TID}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )


async def _plan_turn(pilot, tmp_path, text, *, with_call):
    """Ход, кончившийся «План готов». `with_call` — план предложен в этом
    ходе (рамка из истории), иначе агент предъявил сохранённый заново."""
    scr = await _open(pilot)
    runner = _FakeRunner()
    pilot.app.active_runner = runner
    scr._prepare_for_new_run()
    scr._poll_tick()
    thread = _Thread(tmp_path)
    thread.messages(_reply("u1"))
    if with_call:
        args = json.dumps({"plan": text})
        thread.messages(
            {
                "type": "function_call",
                "name": "turn_propose_plan",
                "call_id": "c1",
                "arguments": args,
            },
            {
                "type": "function_call_output",
                "call_id": "c1",
                "output": json.dumps(
                    {"tool": "turn_propose_plan", "ok": True}
                ),
            },
        )
    thread.record(1, "u1", reason="plan_proposed")
    runner._running = False
    scr._poll_tick()
    await pilot.pause()
    await pilot.pause()
    return scr


def _plan_order(scr):
    from gc_tui.widgets.plan_panel import PlanPanel
    from gc_tui.widgets.turn_line import TurnLine

    kids = list(scr.query_one("#trace").children)
    (line,) = [w for w in kids if isinstance(w, TurnLine)]
    panels = [kids.index(w) for w in kids if isinstance(w, PlanPanel)]
    return kids, line, panels


@pytest.mark.asyncio
async def test_plan_panel_above_fresh_line_only_from_present(tmp_path):
    """«План готов»: рамка над свежей строкой, не задвоена. `/plan` —
    рамка и строка пути под строкой, строка свежая. Переоткрытие — тот же
    порядок, что вживую после хода."""
    text = "1. шаг\n2. проверка"
    _write_plan_thread(tmp_path, text)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _plan_turn(pilot, tmp_path, text, with_call=True)
        kids, line, panels = _plan_order(scr)
        assert len(panels) == 1
        assert panels[0] < kids.index(line)
        assert line.is_fresh
        live = _child_types(scr)

        scr._hide_plan_banner()
        scr._cmd_plan()
        await pilot.pause()
        kids, line, panels = _plan_order(scr)
        assert len(panels) == 2
        assert panels[0] < kids.index(line) < panels[1]
        assert kids[-1] is not line
        assert line.is_fresh

        scr = await _reopen(pilot, tmp_path)
        assert _child_types(scr) == live


@pytest.mark.asyncio
async def test_present_plan_copy_lands_above_fresh_line(tmp_path):
    """Агент предъявил сохранённый план заново: копия рамки встаёт над
    свежей строкой своего хода, а не под ней."""
    text = "1. шаг"
    _write_plan_thread(tmp_path, text)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _plan_turn(pilot, tmp_path, text, with_call=False)
        kids, line, panels = _plan_order(scr)
        assert len(panels) == 1
        assert panels[0] < kids.index(line)
        assert line.is_fresh
