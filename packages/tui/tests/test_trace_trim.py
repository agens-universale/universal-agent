"""Снятие старых ходов окна ленты (спека шага 2 окна ленты, §3.1, §5.3,
§8 «Окно»): пока пользователь прилип к низу и не выделяет текст, лента,
растущая живым путём, снимает самые старые ходы целиком — с запасом
против петли с домонтированием; по возвращении вниз после чтения истории
окно ужимается до лимита. Живые ходы не снимаются никогда."""

from __future__ import annotations

import pytest
from feed_dump import (
    HISTORY_THREAD,
    HistoryFixture,
    feed_live,
    write_history,
)
from gc_testkit.tui_support import wait_until
from gc_tui.services import trace_window_policy as policy
from test_trace_ephemera import _app

_SIZE = (100, 30)


@pytest.fixture(autouse=True)
def _small_window(monkeypatch):
    """Окно в несколько ходов (инвариант лимит ≥ хвост + 2 куска)."""
    monkeypatch.setattr(policy, "TAIL_MIN", 30)
    monkeypatch.setattr(policy, "CHUNK_MIN", 24)
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 80)


def _turn(i: int) -> list[dict]:
    """Ход: реплика, ответ в несколько строк и итог."""
    user, answer = f"m_u{i:04d}", f"msg_a{i:04d}"
    text = "\n".join(f"ответ {i} строка {k}" for k in range(6))
    return [
        {
            "type": "message",
            "payload": {"role": "user", "id": user, "content": f"запрос {i}"},
        },
        {
            "type": "message",
            "payload": {
                "id": answer,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "annotations": [], "text": text}
                ],
            },
        },
        {
            "type": "turn_outcome",
            "after": answer,
            "outcome": {
                "v": 1,
                "status": "done",
                "reason": None,
                "opened_by": user,
                "started_at": "2026-11-13T22:54:10.303Z",
                "ended_at": "2026-11-13T22:54:10.503Z",
                "duration_sec": 0.2,
                "error": None,
                "calls": {},
            },
        },
    ]


def _turns(start: int, stop: int) -> list[dict]:
    return [rec for i in range(start, stop) for rec in _turn(i)]


async def _session(pilot):
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD)
    await pilot.pause()
    return scr


async def _feed(pilot, scr, start: int, stop: int) -> None:
    """Ходы ``start..stop`` живым путём, по ходу за такт."""
    for i in range(start, stop):
        feed_live(scr, _turn(i), splits=())
        await pilot.pause()


def _trace(scr):
    return scr.query_one("#trace")


def _window_turns(scr) -> list[int]:
    return sorted(scr._window_heads)


def _mounted(scr, *, current: bool = True) -> int:
    """Виджетов поддерева ленты в ходах окна (без текущего хода, если
    ``current`` ложно)."""
    turns = _window_turns(scr)
    if not current:
        turns = turns[:-1]
    return sum(
        len(top.walk_children(with_self=True))
        for turn in turns
        for top in scr._window_step_widgets(turn)
    )


async def _user_scroll_up(pilot, scr) -> None:
    _trace(scr).scroll_to(y=0, animate=False)
    await pilot.pause()
    await pilot.pause()
    assert scr._stick_bottom is False


# ---- рост у низа -----------------------------------------------------------


@pytest.mark.asyncio
async def test_grow_bounded(tmp_path):
    """200 ходов живым путём: без текущего хода смонтировано не больше
    «лимит + кусок» после каждого хода; последний ход на месте."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        bound = policy.WINDOW_LIMIT + policy.CHUNK_MIN
        for i in range(200):
            feed_live(scr, _turn(i), splits=())
            await pilot.pause()
            assert _mounted(scr, current=False) <= bound, i
        assert scr._window_first > 150
        assert _window_turns(scr)[-1] == 199
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_trims_only_when_stuck_bottom(tmp_path):
    """Лента отлистана вверх — растущий низ ничего не снимает; вернулась
    к низу — ужатие до лимита."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 6)
        assert scr._window_first == 0
        await _user_scroll_up(pilot, scr)
        await _feed(pilot, scr, 6, 30)
        assert scr._stick_bottom is False
        assert _mounted(scr) > policy.WINDOW_LIMIT + policy.CHUNK_MIN
        assert scr._window_first == 0
        scr._scroll_to_bottom()
        await wait_until(
            pilot,
            lambda: _mounted(scr) <= policy.WINDOW_LIMIT,
            "окно ужато у низа",
        )
        assert scr._window_first > 0
        assert _window_turns(scr)[-1] == 29


@pytest.mark.asyncio
async def test_no_trim_while_selecting(tmp_path, monkeypatch):
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        monkeypatch.setattr(scr, "_ui_frozen_for_select", lambda: True)
        await _feed(pilot, scr, 0, 30)
        assert _mounted(scr) > policy.WINDOW_LIMIT + policy.CHUNK_MIN
        assert scr._window_first == 0
        scr._window_on_bottom()
        await pilot.pause()
        assert scr._window_first == 0


@pytest.mark.asyncio
async def test_trim_hysteresis_no_loop(tmp_path, monkeypatch):
    """Снятие ужимает до лимита, следующее — только сверх «лимит +
    кусок»; домонтирование и снятие друг друга не будят."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        calls = {"prepend": 0, "forget": 0}
        prepend, forget = scr._window_prepend, scr.forget_step

        def counted_prepend(*, focus: bool) -> bool:
            calls["prepend"] += 1
            return prepend(focus=focus)

        def counted_forget(turn: int) -> bool:
            calls["forget"] += 1
            return forget(turn)

        monkeypatch.setattr(scr, "_window_prepend", counted_prepend)
        monkeypatch.setattr(scr, "forget_step", counted_forget)
        for i in range(1, 60):
            await _feed(pilot, scr, i - 1, i)
            if calls["forget"]:
                break
        assert calls["forget"]
        assert _mounted(scr, current=False) <= policy.WINDOW_LIMIT
        settled = dict(calls)
        for _ in range(3):
            await pilot.pause(0.2)
        assert calls == settled
        # Ход после снятия окно не переполняет — снятия нет.
        await _feed(pilot, scr, i, i + 1)
        await pilot.pause(0.2)
        assert calls == settled


# ---- ужатие после чтения ---------------------------------------------------


@pytest.mark.asyncio
async def test_shrink_after_reading_on_return(tmp_path):
    """Сессия открыта хвостом, история дочитана до начала, возврат вниз —
    окно ужато до лимита, последний ход на месте."""
    records = _turns(0, 40)
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(tmp_path / "t"), fixture)
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        assert scr._window_first > 0
        for _ in range(60):
            if scr._window_first == 0:
                break
            await _user_scroll_up(pilot, scr)
            await pilot.pause(0.1)
        assert scr._window_first == 0
        assert _mounted(scr) > policy.WINDOW_LIMIT + policy.CHUNK_MIN
        scr._scroll_to_bottom()
        await wait_until(
            pilot,
            lambda: _mounted(scr) <= policy.WINDOW_LIMIT,
            "окно ужато после чтения",
        )
        assert scr._window_first > 0
        assert _window_turns(scr)[-1] == 39


# ---- что не снимается ------------------------------------------------------


def _pins(scr, widget):
    """Живые ссылки на строку хода (§5.3): (имя, прижать, отпустить)."""

    def focus() -> None:
        widget.can_focus = True
        scr.set_focus(widget)

    def attr(name):
        def pin() -> None:
            setattr(scr, name, widget)

        def unpin() -> None:
            setattr(scr, name, None)

        return pin, unpin

    def keyed(registry):
        return (
            lambda: registry.__setitem__("pin", widget),
            lambda: registry.pop("pin"),
        )

    ephemera = scr._ephemera
    bang_open = ephemera.bang_open_widgets
    return [
        ("фокус", focus, lambda: scr.set_focus(None)),
        ("идущий вызов", *keyed(scr._active_calls)),
        ("отправленная реплика", *keyed(scr._pending_user_widgets)),
        ("свежая строка итога", *attr("_fresh_line")),
        ("строка вопроса", *attr("_ask_human_question_row")),
        ("стрим", *attr("_stream_block")),
        ("строка ответа стрима", *attr("_stream_answer_row")),
        (
            "попытки ответа",
            lambda: scr._submit_solution_widgets.append([widget]),
            lambda: scr._submit_solution_widgets.pop(),
        ),
        (
            "открытый блок «!»",
            lambda: setattr(ephemera, "bang_open_widgets", lambda: [widget]),
            lambda: setattr(ephemera, "bang_open_widgets", bang_open),
        ),
    ]


@pytest.mark.asyncio
async def test_never_trims_current_focused_queue_live_refs(tmp_path):
    """Ход со строкой живого состояния или фокуса, текущий ход и прижатый
    хвост не снимаются: снятие останавливается на первом отказе."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 4)
        await _user_scroll_up(pilot, scr)
        await _feed(pilot, scr, 4, 30)
        first = scr._window_first
        row = scr._window_step_widgets(first)[-1]
        # Ссылки живого состояния ставятся вручную, без бегущей задачи:
        # тик опроса финализировал бы «идущий вызов» как осиротевший.
        # На время пинов тик стоит — проверяется снятие, а не тик.
        scr._poll_tick = lambda: None
        for name, pin, unpin in _pins(scr, row):
            pin()
            assert scr._step_pinned(first), name
            scr._window_on_bottom()
            await pilot.pause()
            assert scr._window_first == first, name
            assert row.is_attached, name
            unpin()
            assert not scr._step_pinned(first), name
        del scr._poll_tick
        last = scr._feed_history.turn_count() - 1
        assert scr._step_pinned(last)
        # Реплика в очереди прижата к низу — в текущем ходе, не снимается.
        queued = scr._mount_user_reply(
            "в очередь", state="queued", client_id="q1"
        )
        scr._scroll_to_bottom()
        await wait_until(
            pilot,
            lambda: _mounted(scr) <= policy.WINDOW_LIMIT,
            "окно ужато у низа",
        )
        assert scr._window_first > first
        assert queued.is_attached
        await _feed(pilot, scr, 30, 60)
        assert queued.is_attached
        assert _window_turns(scr)[-1] == 59


@pytest.mark.asyncio
async def test_admitted_pending_reply_heads_its_turn(tmp_path):
    """Отправленная реплика встаёт раньше, чем ядро открыло её ход, — в
    окне прошлого хода; впуск записью ядра переносит её в начало своего
    хода: снятие прошлого хода её не уносит."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 4)
        scr._pending_wire_cache["c1"] = "запрос 4"
        reply = scr._mount_user_reply(
            "запрос 4", state="pending", client_id="c1"
        )
        await pilot.pause()
        assert reply in scr._window_step_widgets(3)
        await _feed(pilot, scr, 4, 5)
        assert "c1" not in scr._pending_user_widgets
        assert scr._window_heads[4] is reply
        assert reply not in scr._window_step_widgets(3)
        assert scr._window_step_widgets(4)[0] is reply


@pytest.mark.asyncio
async def test_turn_head_removed_outside_window_leaves_it(tmp_path):
    """Голову хода снял не окно — откат реплики (`_cancel_with_undo`):
    ход без виджетов уходит из окна, а соседний ход не тянет за собой
    ленту до конца (граница — живая голова следующего хода)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 4)
        feed_live(scr, _turn(4)[:1], splits=())
        await pilot.pause()
        reply = scr._window_heads[4]
        scr._withdraw_from_history("m_u0004")
        reply.remove()
        await pilot.pause()
        assert 4 not in scr._window_heads
        await _feed(pilot, scr, 5, 6)
        later = scr._window_heads[5]
        assert later not in scr._window_step_widgets(3)
        assert scr._window_step_widgets(5)[0] is later


@pytest.mark.asyncio
async def test_turn_head_removed_at_open_gap_does_not_break_append(tmp_path):
    """Разрыв открыт, за ним — живой ход, его голова — реплика; откат
    снимает её: кусок перед разрывом не встаёт перед снятым виджетом, а
    край разрыва ищется по живым."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 5)
        # Ход 5 без итога: свежая строка остаётся у хода 4, ход 5 снимаем.
        feed_live(scr, _turn(5)[:2], splits=())
        feed_live(scr, _turn(6)[:1], splits=())
        await pilot.pause()
        assert scr._window_forget_step(5)
        await pilot.pause()
        assert (scr._window_gap, scr._window_gap_head()) == (
            range(5, 6),
            scr._window_heads[6],
        )
        reply = scr._window_heads[6]
        scr._withdraw_from_history("m_u0006")
        reply.remove()
        await pilot.pause()
        assert scr._window_gap_head() is None
        last = scr._window_step_widgets(4)[-1]
        assert scr._window_past_gap(last) is False
        assert scr._window_append(focus=False)
        await wait_until(pilot, lambda: not scr._window_busy, "встал")
        assert sorted(scr._window_heads) == [*range(scr._window_first, 6)]
        assert reply not in _trace(scr).children


@pytest.mark.asyncio
async def test_reset_render_state_does_not_trim_old_feed(
    tmp_path, monkeypatch
):
    """Сброс состояния ленты (смена сессии, пересборка) снова «липнет к
    низу», но ужатия по старой ленте с уже пустой моделью нет."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _session(pilot)
        await _feed(pilot, scr, 0, 6)
        await _user_scroll_up(pilot, scr)
        assert scr._window_stuck is False
        calls = []
        monkeypatch.setattr(
            scr, "_window_on_bottom", lambda: calls.append(True)
        )
        scr._reset_render_state()
        assert scr._stick_bottom is True
        assert calls == []
