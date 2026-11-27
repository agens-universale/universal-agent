"""Открытие сессии хвостом окна и засев живого состояния (спека шага 2
окна ленты, §3.1, §5.6, §5.7).

Экран сессии монтирует только хвост истории; старое домонтируется при
прокрутке вверх. Живое состояние экрана (свежая строка итога, смена
модели, вопрос агента) засевается из модели и ссылок отрисовщика;
незаконченный последний ход рисует живой путь."""

from __future__ import annotations

import json

import pytest
from feed_dump import (
    HISTORY_THREAD,
    feed_dump,
    load_history_fixture,
    write_history,
)
from gc_core.persistence.model_choice_record import ModelChoiceRecord
from gc_testkit.thread_writes import model_choice_record
from gc_testkit.tui_support import wait_until
from gc_tui.services import trace_window_policy as policy
from test_feed_parity_baseline import _settled_dump
from test_turn_line_history import (
    OTHER,
    TID,
    _answer,
    _app,
    _FakeRunner,
    _open,
    _reply,
    _Thread,
    _turn_lines,
    _write_thread,
)

_SIZE = (100, 30)
TURNS = 40


@pytest.fixture
def small_window(monkeypatch):
    """Окно в несколько ходов: ход — реплика, вызов, ответ, итог."""
    monkeypatch.setattr(policy, "TAIL_MIN", 30)
    monkeypatch.setattr(policy, "CHUNK_MIN", 24)
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 80)


# ---- тред на диске --------------------------------------------------------


def _call(cid, name="fs_read_file", **args):
    return {
        "type": "function_call",
        "call_id": cid,
        "name": name,
        "arguments": json.dumps(args or {"path": "a.py"}),
        "id": f"fc_{cid}",
    }


def _output(cid):
    return {
        "type": "function_call_output",
        "call_id": cid,
        "output": json.dumps({"ok": True, "content": "x"}),
        "id": f"fco_{cid}",
    }


def _long_answer(i):
    text = "\n".join(f"ответ {i} строка {k}" for k in range(10))
    return _answer(f"a{i}", text)


def _write_turns(tmp_path, n=TURNS, *, calls=True) -> _Thread:
    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    for i in range(n):
        payloads = [_reply(f"u{i}", f"запрос {i}")]
        if calls:
            payloads += [_call(f"c{i}"), _output(f"c{i}")]
        thread.messages(*payloads, _long_answer(i))
        thread.record(i % 10, f"u{i}")
    return thread


# ---- экран ----------------------------------------------------------------


def _trace(scr):
    return scr.query_one("#trace")


def _replies(scr) -> list[str]:
    from gc_tui.widgets.user_reply import UserReplyContainer

    return [
        w.plain_text
        for w in _trace(scr).children
        if isinstance(w, UserReplyContainer)
    ]


async def _filled(pilot, scr) -> None:
    """Догрузка открытия закончилась: кусок не встаёт и не ждёт."""
    for _ in range(3):
        await wait_until(pilot, lambda: not scr._window_busy, "встал")
        await pilot.pause(0.2)


async def _user_scroll(pilot, scr, y: float) -> None:
    _trace(scr).scroll_to(y=y, animate=False)
    await pilot.pause()
    await pilot.pause()


def _focusable(scr) -> list:
    return [w for w in _trace(scr).children if w.can_focus and w.display]


def _row_text(scr, needle: str) -> str:
    (text,) = [t for _cls, t, _ in feed_dump(_trace(scr)) if needle in t]
    return text


# ---- открытие хвостом -----------------------------------------------------


@pytest.mark.asyncio
async def test_open_mounts_tail_only(tmp_path):
    turns = 250
    _write_turns(tmp_path, turns, calls=False)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        trace = _trace(scr)
        replies = _replies(scr)
        assert 0 < len(replies) < turns
        assert replies[-1] == f"запрос {turns - 1}"
        assert scr._window_first > 0
        assert len(trace.walk_children()) <= policy.WINDOW_LIMIT
        assert scr._stick_bottom
        assert trace.scroll_y == trace.max_scroll_y


@pytest.mark.asyncio
async def test_open_tall_tail_mounts_no_chunk(tmp_path):
    """Хвост выше двух экранов: догрузка открытия решает по высоте после
    раскладки — ни одного куска сверху (боевые числа политики). Открытие,
    как из списка сессий: хвост монтируется под другим экраном, раскладка
    ленты — только когда экран сессии снова наверху."""
    from textual.screen import Screen

    _write_turns(tmp_path, 60, calls=False)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        await pilot.app.push_screen(Screen())
        await pilot.pause()
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause(0.3)
        pilot.app.pop_screen()
        await pilot.pause()
        tail = policy.tail_steps(
            scr._window_estimates(scr._window_model()), policy.TAIL_MIN
        )
        await _filled(pilot, scr)
        trace = _trace(scr)
        assert trace.max_scroll_y >= 2 * trace.container_size.height
        assert scr._window_first == tail.start


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_open_then_scroll_reveals_history(tmp_path):
    _write_turns(tmp_path)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        assert scr._window_first > 0
        for _ in range(60):
            if scr._window_first == 0:
                break
            await _user_scroll(pilot, scr, 0)
            await pilot.pause(0.1)
        assert scr._window_first == 0
        # Второй край окна (§5.11): низ прочитанного снят до живого хода.
        gap = scr._window_gap
        assert _replies(scr) == [
            f"запрос {i}" for i in range(TURNS) if i not in gap
        ]
        assert _replies(scr)[-1] == f"запрос {TURNS - 1}"


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_prepend_at_top_keeps_reading_row(tmp_path, monkeypatch):
    """Лента в самом верху (видна шапка начала сессии над окном): кусок
    сверху встаёт под шапку, а на месте остаётся строка, которую читает
    пользователь, — прокрутка сдвигается на высоту куска и цепочки кусков
    нет."""
    _write_turns(tmp_path)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        trace = _trace(scr)
        monkeypatch.setattr(scr, "_window_prefetch", lambda: None)
        await _user_scroll(pilot, scr, 0)
        assert scr._stick_bottom is False
        assert trace.scroll_y == 0
        row = scr._window_top()
        at = row.region.y
        first = scr._window_first
        assert scr._window_prepend(focus=False)
        await wait_until(pilot, lambda: not scr._window_busy, "встал")
        await pilot.pause()
        assert scr._window_first < first
        assert trace.scroll_y > 0
        assert row.region.y == at


async def _dump_open(tmp_path, monkeypatch, tail_min: int):
    monkeypatch.setattr(policy, "TAIL_MIN", tail_min)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        return await _settled_dump(pilot)


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_open_parity_tail(tmp_path, monkeypatch):
    _write_turns(tmp_path)
    full = await _dump_open(tmp_path, monkeypatch, 10**9)
    tail = await _dump_open(tmp_path, monkeypatch, 30)
    assert 0 < len(tail) < len(full)
    assert full[-len(tail) :] == tail


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_open_seeds_fresh_turn_line_and_model_mark(tmp_path):
    from gc_tui.widgets.model_mark import ModelMark

    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    thread.turn(0)
    model_choice_record(
        str(tmp_path), "cli", TID, ModelChoiceRecord("max", "strong", "M")
    )
    for i in range(1, TURNS):
        thread.messages(_reply(f"u{i}"), _call(f"c{i}"), _output(f"c{i}"))
        thread.messages(_long_answer(i))
        thread.record(i % 10, f"u{i}")
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        lines = _turn_lines(scr)
        assert scr._fresh_turn_line() is lines[-1]
        assert lines[-1].opened_by == f"u{TURNS - 1}"
        assert [line for line in lines if line.is_fresh] == [lines[-1]]
        # Черта смены модели — в ходе вне окна, лента знает её по модели.
        assert not list(_trace(scr).query(ModelMark))
        assert scr._last_model_choice() == "max"


# ---- вопрос агента --------------------------------------------------------


def _question_thread(tmp_path, *, outcome: bool) -> _Thread:
    thread = _write_turns(tmp_path, 3)
    thread.messages(
        _reply("uq", "спроси меня"),
        _call("q1", name="turn_ask_human", question="А или Б?"),
    )
    if outcome:
        thread.record(9, "uq", status="stopped", reason="user_stop")
    return thread


@pytest.mark.asyncio
async def test_open_pending_question_banner(tmp_path):
    """Последний ход ждёт ответа, агент жив: баннер вопроса и скрытая
    строка вопроса — как при полном открытии."""
    _question_thread(tmp_path, outcome=False)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        scr = await _open(pilot)
        assert scr._ask_human_banner is not None
        row = scr._question_rows["q1"]
        assert row.has_class("hidden")
        assert row is scr._ask_human_question_row


@pytest.mark.asyncio
@pytest.mark.parametrize("outcome", [False, True])
async def test_open_dangling_question_interrupted(tmp_path, outcome):
    """Вопрос без ответа и без живого агента — «прервано» под строкой
    вопроса; ход с записью итога рисует отрисовщик кусков, без неё —
    живой путь."""
    from gc_tui.services.trace_render import QUESTION_INTERRUPTED

    _question_thread(tmp_path, outcome=outcome)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await pilot.pause()
        assert scr._ask_human_banner is None
        assert QUESTION_INTERRUPTED in _row_text(scr, "А или Б?")
        assert not scr._question_rows["q1"].has_class("hidden")


# ---- живой путь после открытия ---------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_live_events_after_open_append_and_count(tmp_path):
    from gc_tui.widgets.user_reply import UserReplyContainer

    thread = _write_turns(tmp_path)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        first = scr._window_first
        await _user_scroll(pilot, scr, 0)
        await wait_until(
            pilot, lambda: scr._window_first < first, "кусок сверху"
        )
        await _filled(pilot, scr)
        # Домонтированные сверху ходы новыми не считаются.
        assert scr._new_replies == 0

        thread.messages(_answer("late", "поздний ответ"))
        scr._poll_trace_tail()
        await pilot.pause()
        assert scr._new_replies == 1
        assert "поздний ответ" in feed_dump(_trace(scr))[-1][1]

        thread.messages(_reply("u_new", "новый ход"), _answer("a_new"))
        scr._poll_trace_tail()
        await pilot.pause()
        model = scr._feed_history
        head = scr._window_heads[model.turn_count() - 1]
        assert isinstance(head, UserReplyContainer)
        assert head.plain_text == "новый ход"


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_rebuild_midrun_unfinished_turn_live(tmp_path, monkeypatch):
    from gc_tui.screens import trace_window

    thread = _write_turns(tmp_path, 20)
    thread.messages(_reply("u_live", "идёт"), _call("c_live"))
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        pilot.app.active_runner = _FakeRunner()
        scr._prepare_for_new_run()
        drawn: list[range] = []
        render = trace_window.render_steps

        def spy(host, model, turns, **kw):
            drawn.append(turns)
            return render(host, model, turns, **kw)

        monkeypatch.setattr(trace_window, "render_steps", spy)
        scr._rebuild_from_store()
        await pilot.pause()

        live_turn = scr._feed_history.turn_count() - 1
        assert drawn
        assert all(turns.stop <= live_turn for turns in drawn)
        assert drawn[0].stop == live_turn
        assert "c_live" in scr._active_calls
        assert _replies(scr)[-1] == "идёт"

        thread.messages(_output("c_live"))
        scr._poll_trace_tail()
        await pilot.pause()
        assert "c_live" not in scr._active_calls


@pytest.mark.asyncio
@pytest.mark.parametrize("rebuild", [False, True])
async def test_open_after_turn_end_compaction_all_from_chunks(
    tmp_path, monkeypatch, rebuild
):
    """Тред кончается сжатием после итога хода: последний ход закончен —
    открытие и пересборка рисуют все ходы кусками, живого хода нет."""
    from gc_tui.screens import trace_window

    write_history(str(tmp_path), load_history_fixture("compaction"))
    drawn: list[range] = []
    render = trace_window.render_steps

    def spy(host, model, turns, **kw):
        drawn.append(turns)
        return render(host, model, turns, **kw)

    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        if rebuild:
            assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
            await pilot.pause()
            drawn.clear()
            monkeypatch.setattr(trace_window, "render_steps", spy)
            scr._rebuild_from_store()
        else:
            monkeypatch.setattr(trace_window, "render_steps", spy)
            assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause()
        assert scr._feed_history.open_turn() is None
        assert drawn[0].stop == scr._feed_history.turn_count()


# ---- окно экрана сессии ---------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_event_up_at_window_top_mounts_history(tmp_path, monkeypatch):
    _write_turns(tmp_path)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        # Фокус на верх окна прокручивает к нему — домонтирование по
        # прокрутке здесь не проверяется.
        monkeypatch.setattr(scr, "_window_prefetch", lambda: None)
        top = _focusable(scr)[0]
        top.focus()
        await pilot.pause()
        first = scr._window_first
        await pilot.press("up")
        await wait_until(
            pilot,
            lambda: scr.focused is not top and scr.focused is not None,
            "фокус на куске сверху",
        )
        events = _focusable(scr)
        assert scr._window_first < first
        assert events.index(scr.focused) == events.index(top) - 1


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_switch_resets_window_state(tmp_path):
    _write_turns(tmp_path)
    _write_thread(tmp_path, OTHER, tui={"status": "completed"})
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        gen = scr._window_gen
        scr._window_stuck = False
        scr._window_trim_due = True
        scr.attach_active_task(OTHER, is_resume=True)
        await pilot.pause()
        assert scr._window_gen > gen
        assert scr._window_heads == {}
        assert scr._window_first == 0
        assert scr._window_stuck and not scr._window_trim_due


@pytest.mark.asyncio
@pytest.mark.usefixtures("small_window")
async def test_new_session_resets_window_state(tmp_path):
    """/new посреди встающего куска — окно прежней сессии не действует:
    иначе догрузка по пустой модели (IndexError), а кусок прежнего окна
    встаёт новой сессии."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    _write_turns(tmp_path)
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        assert scr._window_first > 0
        gen = scr._window_gen
        assert scr._window_prepend(focus=False)
        assert scr._window_busy
        scr._switch_to_empty_state()
        assert scr._window_gen > gen
        assert (scr._window_first, scr._window_gap) == (0, range(0))
        assert not scr._window_busy
        # Кусок прежнего окна домонтировался и отпущен — его settle
        # выброшен поколением.
        await pilot.pause(0.5)
        scr._window_fill()
        await pilot.pause(0.3)
        assert not scr._window_busy
        assert scr._window_heads == {}
        assert not _trace(scr).query(UserReplyContainer)


@pytest.mark.asyncio
async def test_history_block_toggle_routes(tmp_path):
    """Блок вызовов хода, нарисованного отрисовщиком кусков, раскрывается
    на экране сессии; живой реестр видов его не держит."""
    from gc_tui.messages import BlockToggle
    from gc_tui.services.history_render import history_level

    thread = _write_turns(tmp_path, 3)
    reads = [p for i in range(5) for p in (_call(f"r{i}"), _output(f"r{i}"))]
    thread.messages(_reply("u3", "читай"), *reads, _answer("a3", "Готово."))
    thread.record(3, "u3")
    async with _app(tmp_path).run_test(size=_SIZE) as pilot:
        scr = await _open(pilot)
        await _filled(pilot, scr)
        level = history_level(3)
        assert level in scr._history_views
        assert level not in scr._block_views
        before = feed_dump(_trace(scr))
        scr.post_message(BlockToggle(level, "block:0:top"))
        await pilot.pause()
        await pilot.pause()
        assert feed_dump(_trace(scr)) != before
