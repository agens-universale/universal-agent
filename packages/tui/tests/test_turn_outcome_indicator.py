"""Строка последнего хода — из записи итога ядра и часов хода ядра.

Исход, время конца, длительность и причина ошибки последнего хода — только
из записи `turn_outcome` в файле треда (решение плана 5): вживую и после
переоткрытия одна свежая строка в ленте (S10), индикатора итога нет.
Живой счёт индикатора — от часов хода ядра
(`runner.turn_effective_start()`): ожидание человека в него не входит, и
ответ на вопрос агента счёт не обнуляет. Нет записи — прежнее правило по
статусу сессии, без времени.
"""

from __future__ import annotations

import json
from datetime import timedelta

import pytest
from gc_core.execution.environment import utc_now
from gc_core.execution.turn_clock import TurnClock
from gc_core.persistence.turn_outcome import USER_STOP, TurnOutcome
from gc_testkit.develop_files import append_new_thread_messages
from gc_testkit.thread_writes import turn_outcome_record, turn_records
from gc_testkit.tui_fixtures.stub_executor import StubExecutor
from gc_testkit.tui_support import tui_settings, wait_until
from gc_tui.services.relative_time import format_turn_clock, parse_iso_utc
from gc_tui.services.stores.record_view import tui_section

TID = "tout0000000000000000000000000001a"
OTHER = "tout0000000000000000000000000002b"
STARTED = "2026-10-22T11:32:00Z"
ENDED = "2026-10-22T11:32:12Z"


def _hm(iso: str) -> str:
    return parse_iso_utc(iso).astimezone().strftime("%H:%M")


@pytest.fixture(autouse=True)
def _pin_now(monkeypatch):
    """«Сейчас» строки — минута после конца хода: время без даты."""
    from gc_tui.services import relative_time

    now = parse_iso_utc(ENDED).astimezone() + timedelta(minutes=1)
    monkeypatch.setattr(relative_time, "_now_local", lambda: now)


def _write_thread(tmp_path, tid=TID, tui=None):
    meta = {"thread_id": tid, "system_id": "cli"}
    if tui is not None:
        meta["tui"] = tui
    (tmp_path / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8"
    )
    (tmp_path / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


def _messages(tmp_path, *payloads, start_seq=0):
    append_new_thread_messages(
        str(tmp_path), "cli", TID, list(payloads), set(), start_seq=start_seq
    )


def _reply(mid, text="вопрос"):
    return {"id": mid, "role": "user", "content": text}


def _answer(mid, text="ответ"):
    return {
        "id": mid,
        "type": "message",
        "role": "assistant",
        "content": [{"type": "output_text", "text": text}],
    }


def _record(tmp_path, *, seq, status="done", reason=None, **kw):
    outcome = TurnOutcome(
        status=status,
        reason=reason,
        opened_by=kw.get("opened_by"),
        started_at=STARTED,
        ended_at=kw.get("ended", ENDED),
        duration_sec=kw.get("dur", 12.0),
        error=kw.get("error"),
    )
    turn_outcome_record(str(tmp_path), "cli", TID, outcome)


def _records(tmp_path, tid):
    path = tmp_path / f"data_cli_{tid}.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    return [json.loads(line) for line in lines if line.strip()]


def _outcomes(tmp_path, tid):
    return [r["outcome"] for r in turn_records(str(tmp_path), "cli", tid)]


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _fresh_line(scr):
    from gc_tui.widgets.turn_line import TurnLine

    lines = list(scr.query(TurnLine).filter(".-latest"))
    return lines[-1] if lines else None


def _line_shown(scr) -> bool:
    """Итог хода стоит свежей строкой в ленте, индикатор ушёл."""
    return scr._thinking is None and _fresh_line(scr) is not None


def _text(scr) -> str:
    """Свежая строка последнего хода: голова, сводка, причина."""
    assert _line_shown(scr)
    return _fresh_line(scr).render().plain


def _live(scr) -> str:
    """Живой индикатор работы."""
    assert scr._thinking is not None
    return scr._thinking._render_text().plain


async def _reopen(pilot, tmp_path, tid):
    """Переоткрыть тред: через другой тред, как из `/sessions`."""
    _write_thread(tmp_path, OTHER, tui={"status": "completed"})
    scr = pilot.app.screen
    scr.attach_active_task(OTHER, is_resume=True)
    scr.attach_active_task(tid, is_resume=True)
    await pilot.pause()
    return scr


class _ClockRunner:
    """Живой прогон с часами хода ядра на подменной монотонике."""

    _thread_kind = "run"
    _stop_requested = False

    def __init__(self, tid, clock=None):
        self._last_target = (tid, "cli")
        self.clock = clock
        self.running = True
        self.run_seq = 1

    def is_running(self):
        return self.running

    def turn_effective_start(self):
        return self.clock.effective_start() if self.clock else None

    def submit_ask_human_reply(self, _answer):
        pass

    def cancel_ask_human(self):
        pass

    def cancel_confirm(self):
        pass

    def add_message(self, _message):
        pass

    def stop(self):
        self.running = False


def _fake_clock():
    now = {"t": 100.0}
    clock = TurnClock.start(mono=lambda: now["t"], wall=utc_now)
    return clock, now


# --- голова индикатора: время из записи -----------------------------------


def test_stopping_blink_has_no_time():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="stopping")
    assert ind._render_text().plain == "■"


def test_live_count_follows_started_at_setter():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(started_at=0.0, clock=lambda: 50.0)
    ind.set_started_at(45.0)
    assert "(5с)" in ind._render_text().plain


def test_indicator_state_is_public():
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(state="stopping")
    assert ind.state == "stopping"
    ind.set_state("thinking")
    assert ind.state == "thinking"


def test_no_count_until_turn_clock_exists():
    """Часов хода ещё нет — счёта нет (а не счёт от отправки, который
    потом откатился бы к нулю)."""
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    ind = ThinkingIndicator(started_at=0.0, clock=lambda: 50.0)
    ind.set_started_at(None)
    assert ind._render_text().plain.endswith("…")


# --- ход через раннер и заглушку исполнителя -------------------------------


async def _send(pilot, text="привет"):
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.press("enter")
    await pilot.pause()
    return pilot.app.screen


def _late_attach(monkeypatch, tmp_path):
    """Быстрый исполнитель: тред открывается не раньше, чем исполнитель
    записал реплику, если ход к этому моменту уже стартовал."""
    import time

    from gc_tui.screens.main import TraceScreen

    attach = TraceScreen.attach_active_task

    def late(self, task_id, **kw):
        data = tmp_path / f"data_cli_{task_id}.jsonl"
        deadline = time.monotonic() + 1.0
        while time.monotonic() < deadline and not (
            data.exists() and '"user"' in data.read_text(encoding="utf-8")
        ):
            time.sleep(0.01)
        return attach(self, task_id, **kw)

    monkeypatch.setattr(TraceScreen, "attach_active_task", late)


@pytest.mark.asyncio
async def test_done_turn_line_from_record_live_and_reopen(
    tmp_path, monkeypatch
):
    """Первый ход автосозданного треда: реплика одна и сверена с
    отправленной, строка итога свежая — и после переоткрытия."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    _late_attach(monkeypatch, tmp_path)
    app = _app(tmp_path)
    app._executor_factory = lambda: StubExecutor(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = await _send(pilot)
        tid = scr._active_task_id
        head = f"✓ Выполнил за 12с · {_hm(ENDED)}"
        await wait_until(
            pilot,
            lambda: _line_shown(scr) and _text(scr).startswith(head),
            "строка итога из записи",
        )
        assert len(scr.query(UserReplyContainer)) == 1
        assert scr._pending_user_widgets == {}
        tui = tui_section(scr._store.get_record(tid))
        assert tui["status"] == "completed"
        assert "last_error" not in tui
        assert set(tui["last_indicator"]) == {"edits"}

        scr = await _reopen(pilot, tmp_path, tid)
        assert _text(scr).startswith(head)


@pytest.mark.asyncio
async def test_stop_blinks_then_cancelled_at_stop_moment(tmp_path):
    stubs: list = []

    def factory():
        stubs.append(StubExecutor(threads_dir=str(tmp_path), step_delay=0.3))
        return stubs[-1]

    _write_thread(tmp_path)
    app = _app(tmp_path)
    app._executor_factory = factory
    async with app.run_test() as pilot:
        # Тред открыт до отправки: реплика сверяется с отправленной (у
        # автосоздания треда заглушка пишет реплику раньше сверки).
        pilot.app.screen.attach_active_task(TID, is_resume=True)
        scr = await _send(pilot)
        tid = scr._active_task_id
        await wait_until(
            pilot,
            lambda: pilot.app.active_runner.produced_output(),
            "агент начал ход",
        )
        old = pilot.app.active_runner
        scr._cmd_stop()
        assert scr._thinking._state == "stopping"
        assert _live(scr) in ("■", " ")
        await wait_until(
            pilot, lambda: not old.is_running(), "поток хода вышел"
        )
        await wait_until(
            pilot,
            lambda: (
                bool(_outcomes(tmp_path, tid))
                and _line_shown(scr)
                and "·" in _text(scr)
            ),
            "ровный итог стопа со временем",
        )
        (outcome,) = _outcomes(tmp_path, tid)
        assert (outcome["status"], outcome["reason"]) == (
            "stopped",
            USER_STOP,
        )
        assert outcome["ended_at"] == stubs[0].clock.ended_at
        clock_time = format_turn_clock(outcome["ended_at"])
        assert _text(scr) == f"■ Отменено · {clock_time}"

        scr = await _reopen(pilot, tmp_path, tid)
        assert _text(scr) == f"■ Отменено · {clock_time}"


@pytest.mark.asyncio
async def test_quit_mid_turn_writes_stop_record_and_waits(tmp_path):
    """Выход посреди хода: ядро успевает закрыть ход записью, поток
    раннера дождался (решение плана 6)."""
    app = _app(tmp_path)
    app._executor_factory = lambda: StubExecutor(
        threads_dir=str(tmp_path), step_delay=0.3
    )
    async with app.run_test() as pilot:
        scr = await _send(pilot)
        tid = scr._active_task_id
        runner = pilot.app.active_runner
        await wait_until(
            pilot,
            lambda: pilot.app.active_runner.produced_output(),
            "агент начал ход",
        )
        pilot.app.exit()
        await pilot.pause()
    assert runner._thread.is_alive() is False
    (outcome,) = _outcomes(tmp_path, tid)
    assert (outcome["status"], outcome["reason"]) == ("stopped", USER_STOP)


class _StreamingStub(StubExecutor):
    """Ход стримит ответ: дельта приходит и после стопа — посреди
    ожидания выхода, через тот же мост `call_from_thread`, что у прода."""

    def __init__(self, app, **kw):
        from gc_tui.services.stream_pump import StreamPump

        super().__init__(**kw)
        self._pump = StreamPump(
            app._stream_sink,
            signal=lambda: app.call_from_thread(app._stream_signal),
            throttle_s=0.0,
        )

    def _record_outcome(self, system, thread_id, written):
        if self.stopped:
            self._pump.on_delta("answer", "хвост ответа")
        super()._record_outcome(system, thread_id, written)


@pytest.mark.asyncio
async def test_quit_during_stream_does_not_freeze(tmp_path):
    """Дельта стрима посреди ожидания выхода: мост на UI-поток не
    упирается в ожидание — выход быстрый, ход закрыт записью."""
    import time

    app = _app(tmp_path)
    app._executor_factory = lambda: _StreamingStub(
        app, threads_dir=str(tmp_path), step_delay=0.3
    )
    async with app.run_test() as pilot:
        scr = await _send(pilot)
        tid = scr._active_task_id
        runner = pilot.app.active_runner
        await wait_until(
            pilot,
            lambda: pilot.app.active_runner.produced_output(),
            "агент начал ход",
        )
        began = time.monotonic()
        pilot.app.exit()
        await pilot.pause()
    assert time.monotonic() - began < 1.5
    assert runner._thread.is_alive() is False
    (outcome,) = _outcomes(tmp_path, tid)
    assert (outcome["status"], outcome["reason"]) == ("stopped", USER_STOP)


# --- ожидание человека ------------------------------------------------------


@pytest.mark.asyncio
async def test_answer_keeps_turn_clock_and_duration_without_wait(tmp_path):
    """Вопрос агента, ответ через 30 с: счёт после ответа — от часов хода
    (без 30 с), не с нуля; итог — длительность из записи."""
    from gc_tui.widgets.ask_human_banner import AskHumanBanner

    _write_thread(tmp_path)
    clock, now = _fake_clock()
    app = _app(tmp_path)
    runner = _ClockRunner(TID, clock)
    app.active_runner = runner
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        # Вопрос приходит хвостом живого хода: его владелец — этот раннер.
        StubExecutor(
            threads_dir=str(tmp_path), scenario="ask_human_flow"
        ).record(["спроси меня"], thread_id=TID)
        await wait_until(
            pilot,
            lambda: len(pilot.app.query(AskHumanBanner)) == 1,
            "баннер вопроса",
        )
        now["t"] = 105.0
        clock.begin_wait()
        now["t"] = 135.0
        clock.end_wait()
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "A"
        await pilot.press("enter")
        await pilot.pause()

        assert scr._thinking._started_at == 130.0

        now["t"] = 142.0
        clock.close()
        _record(tmp_path, seq=4, dur=round(clock.elapsed(), 1))
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        # Ответ на вопрос в файл не лёг (вопрос сыгран заглушкой заранее):
        # реплика ответа ждёт сверки, строка встаёт осевшей — проверяется
        # её голова.
        from gc_tui.widgets.turn_line import TurnLine

        line = list(scr.query(TurnLine))[-1]
        assert line.text.plain == f"✓ Выполнил за 12с · {_hm(ENDED)}"


@pytest.mark.asyncio
async def test_live_count_waits_for_core_clock(tmp_path):
    """Сборка исполнителя (MCP) идёт, часов хода ядра нет: индикатор без
    счёта; часы появились — счёт от них."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply("u1"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        scr._prepare_for_new_run()
        scr._reset_thinking_timer()
        runner = _ClockRunner(TID)
        pilot.app.active_runner = runner
        scr._poll_active()
        assert scr._thinking._started_at is None
        assert "с)" not in _live(scr)
        runner.clock, _now = _fake_clock()
        scr._poll_active()
        assert scr._thinking._started_at == 100.0


@pytest.mark.asyncio
async def test_confirm_wait_does_not_count(tmp_path):
    """Подтверждение: пока человек решает, счёт хода стоит."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply("u1"))
    clock, now = _fake_clock()
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        scr._prepare_for_new_run()
        pilot.app.active_runner = _ClockRunner(TID, clock)
        scr._poll_active()
        now["t"] = 105.0
        clock.begin_wait()
        now["t"] = 135.0
        scr._poll_active()
        assert scr._thinking._started_at == 130.0
        clock.end_wait()
        now["t"] = 140.0
        scr._poll_active()
        assert scr._thinking._started_at == 130.0


# --- исходы из записи -------------------------------------------------------


async def _finish_turn(pilot, tmp_path, **record):
    """Ход прошёл через экран; запись итога легла до спадающего фронта."""
    _write_thread(tmp_path)
    _messages(tmp_path, _reply("u1"))
    scr = pilot.app.screen
    scr.attach_active_task(TID)
    scr._prepare_for_new_run()
    scr._reset_thinking_timer()
    runner = _ClockRunner(TID)
    pilot.app.active_runner = runner
    scr._poll_active()
    _messages(tmp_path, _answer("a1"), start_seq=1)
    _record(tmp_path, seq=2, **record)
    runner.running = False
    scr._poll_active()
    await pilot.pause()
    return scr


@pytest.mark.asyncio
async def test_error_turn_from_record_with_reason(tmp_path):
    reason = "RuntimeError: " + "сбой " * 60
    async with _app(tmp_path).run_test() as pilot:
        pilot.app.last_runner_error = None
        scr = await _finish_turn(
            pilot,
            tmp_path,
            status="error",
            ended="2026-10-22T11:50:00Z",
            error={"type": "RuntimeError", "message": reason},
        )
        assert _text(scr) == f"✕ Ошибка · {_hm('2026-10-22T11:50:00Z')}"
        # Длинная причина у свежей строки развёрнута под ней целиком.
        (body,) = scr.query(".turn-reason")
        text = str(body.query_one(".expandable-text").render())
        assert " ".join(text.split()) == " ".join(reason.split())
        tui = tui_section(scr._store.get_record(TID))
        assert tui["status"] == "failed"
        assert "last_error" not in tui

        live = _text(scr)
        scr = await _reopen(pilot, tmp_path, TID)
        assert _text(scr) == live


@pytest.mark.asyncio
async def test_limit_turn_from_record(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        scr = await _finish_turn(
            pilot,
            tmp_path,
            status="stopped",
            reason="step_limit",
            ended="2026-10-22T12:02:00Z",
        )
        assert _text(scr) == (
            "⏹ Остановлено: превышен лимит шагов · "
            f"{_hm('2026-10-22T12:02:00Z')}"
        )
        assert tui_section(scr._store.get_record(TID))["status"] == "stopped"


@pytest.mark.asyncio
async def test_turn_without_record_falls_back_without_time(tmp_path):
    """Ход записи не оставил (хранилище без записей): прежний путь из
    раннера, без времени."""
    async with _app(tmp_path).run_test() as pilot:
        _write_thread(tmp_path)
        _messages(tmp_path, _reply("u1"))
        _record(tmp_path, seq=1)  # прошлый ход
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        scr._prepare_for_new_run()
        scr._reset_thinking_timer()
        runner = _ClockRunner(TID)
        pilot.app.active_runner = runner
        scr._poll_active()
        _messages(tmp_path, _reply("u2"), _answer("a2"), start_seq=1)
        pilot.app.last_terminal_outcome = "step_limit"
        runner.running = False
        scr._poll_active()
        await pilot.pause()
        assert _text(scr) == "⏹ Остановлено: превышен лимит шагов"


# --- переоткрытие -----------------------------------------------------------


@pytest.mark.asyncio
async def test_running_thread_without_live_turn_is_interrupted(tmp_path):
    """Ход 1 записан, ход 2 начат и оборван: «■ Прервано» без времени,
    не итог прошлого хода."""
    _write_thread(tmp_path, tui={"status": "running"})
    _messages(tmp_path, _reply("u1"), _answer("a1"))
    _record(tmp_path, seq=2)
    _messages(tmp_path, _reply("u2"), start_seq=2)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _text(scr) == "■ Прервано"


@pytest.mark.asyncio
async def test_plan_continuation_cut_off_is_interrupted(tmp_path):
    """Ход с планом записан, континуация (только техническая реплика)
    оборвана без записи: статус «running» без живого хода — «■ Прервано»,
    а не «✓ План готов» прошлого хода."""
    from gc_core.message_policy.user_message_policy import (
        wrap_technical_user_content,
    )

    _write_thread(tmp_path, tui={"status": "running"})
    _messages(tmp_path, _reply("u1"), _answer("a1"))
    _record(tmp_path, seq=2, reason="plan_proposed")
    continuation = {
        "id": "t1",
        "role": "user",
        "content": wrap_technical_user_content(
            "план утверждён", technical_type="technical"
        ),
    }
    _messages(tmp_path, continuation, start_seq=2)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _text(scr) == "■ Прервано"


@pytest.mark.asyncio
async def test_quit_without_record_reopens_as_cancelled(tmp_path):
    """Выход посреди хода, ядро не успело записать итог: ход закончил
    пользователь — «■ Отменено» (статус «stopped» пишет выход), не
    «■ Прервано»; «прервано» — только для убитого процесса."""
    _write_thread(tmp_path, tui={"status": "stopped"})
    _messages(tmp_path, _reply("u1"), _answer("a1"))
    _record(tmp_path, seq=2)
    _messages(tmp_path, _reply("u2"), start_seq=2)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _text(scr) == "■ Отменено"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("status", "line"),
    [("completed", "✓ Выполнил"), ("failed", "✕ Ошибка")],
)
async def test_old_meta_without_records_uses_status_word(
    tmp_path, status, line
):
    _write_thread(
        tmp_path,
        tui={
            "status": status,
            "last_indicator": {"kind": "stopped_steps", "elapsed": 7},
            "last_error": "ReadError: boom",
        },
    )
    _messages(tmp_path, _reply("u1"), _answer("a1"))
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()
        assert _text(scr) == line


# --- хвост из очереди между тиками ------------------------------------------


@pytest.mark.asyncio
async def test_tail_started_between_ticks_settles_previous_run(tmp_path):
    """Хвост стартовал отдельным прогоном между тиками: раннер «всё ещё
    бежит», но номер прогона сменился — конец прошлого хода закрыт."""
    _write_thread(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(TID)
        settled: list = []
        original = scr._on_run_finished

        def _spy(runner, kind):
            settled.append(runner.run_seq)
            original(runner, kind)

        scr._on_run_finished = _spy
        scr._prepare_for_new_run()
        runner = _ClockRunner(TID)
        pilot.app.active_runner = runner
        scr._poll_active()
        runner.run_seq = 2
        scr._poll_active()
        assert settled == [2]
        assert tui_section(scr._store.get_record(TID))["status"] == "running"
        runner.running = False
        scr._poll_active()
        assert len(settled) == 2
