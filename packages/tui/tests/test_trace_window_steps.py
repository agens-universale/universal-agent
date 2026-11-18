"""Шаг хода — единица окна ленты (спека шага 2 окна ленты, §5.3).

В реальной работе агента ход один и огромный: сотня вызовов тулов. Окно,
у которого минимальная единица — ход, монтировало такой ход одним куском
(полторы тысячи виджетов, полторы секунды без отклика). Ход делится на
шаги по границам вызовов (`trace_steps`), окно домонтирует и снимает шаги:
за одну подгрузку — не больше ``CHUNK_MAX`` виджетов, читаемая строка на
месте. Ход, нарисованный по шагам в любом порядке, выглядит так же, как
целиком."""

from __future__ import annotations

import re

import pytest
from feed_dump import (
    HISTORY_THREAD,
    HistoryFixture,
    feed_dump,
    feed_live,
    write_history,
)
from gc_testkit.tui_support import wait_until
from gc_tui.services import trace_window_policy as policy
from gc_tui.services.trace_steps import SPLIT_FROM_ITEMS, STEP_MIN_ITEMS
from test_trace_ephemera import _app
from test_trace_window_far_edge import _call, _message
from textual import events
from textual.app import App
from textual.containers import VerticalScroll

_SIZE = (100, 30)
#: Вызовов в гигантском ходе и вызовов между прозой агента.
CALLS = 150
PROSE_EVERY = 3


# ---- записи ----------------------------------------------------------------


def _prose(i: int, k: int) -> dict:
    return _message(
        {
            "id": f"msg_p{i:02d}_{k:03d}",
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [
                {
                    "type": "output_text",
                    "annotations": [],
                    "text": f"Ход {i}: смотрю шаг {k}.",
                }
            ],
        }
    )


def _outcome(user: str, after: str) -> dict:
    return {
        "type": "turn_outcome",
        "after": after,
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
    }


def _giant_turn(i: int, calls: int, *, outcome: bool = True) -> list[dict]:
    """Ход агента: реплика, ``calls`` вызовов с прозой через
    ``PROSE_EVERY`` и ответ; ``outcome`` — с записью итога."""
    user = f"m_u{i:04d}"
    records = [
        _message({"role": "user", "id": user, "content": f"запрос {i}"})
    ]
    for k in range(calls):
        if k % PROSE_EVERY == 0:
            records.append(_prose(i, k))
        records += _call(i, k)
    answer = _prose(i, calls)
    records.append(answer)
    if outcome:
        records.append(_outcome(user, answer["payload"]["id"]))
    return records


def _session() -> list[dict]:
    """Два гигантских хода и живой (без итога) короткий — пользователь
    стоит у живого хода."""
    return [
        *_giant_turn(0, CALLS),
        *_giant_turn(1, CALLS),
        *_giant_turn(2, 3, outcome=False),
    ]


def _model(records: list[dict]):
    from gc_tui.services.history_model import HistoryInputs, build_history

    return build_history(
        [{"messages": records, "compacted_by": None}],
        inputs=HistoryInputs(root=None, injected_fields=()),
    )


# ---- модель: шаги ----------------------------------------------------------


def test_giant_turn_splits_into_steps_at_tool_boundaries():
    model = _model(_session())
    assert model.turn_count() == 3
    # Гигантский ход — много шагов, короткий живой — один.
    assert len(range(model.turn_step(0), model.turn_step(1))) >= 10
    assert len(range(model.turn_step(2), model.turn_step(3))) == 1
    # Начало хода — всегда начало шага.
    assert set(model.turn_starts) <= set(model.step_starts)
    heads = {model.turn_step(t) for t in range(model.turn_count())}
    for s in range(model.step_count()):
        span = model.step_range(s)
        assert model.item_step(span.start) == s
        if s in heads:
            continue
        # Шаг внутри хода начинается прозой, прошлый — не короче
        # минимума, а ход до него — длиннее порога деления.
        first = model.items[span.start].event
        assert first is not None and first.kind == "agent"
        turn = model.step_turn(s)
        assert turn == model.step_turn(s - 1)
        assert len(model.step_range(s - 1)) >= STEP_MIN_ITEMS
        assert span.start - model.turn_starts[turn] >= SPLIT_FROM_ITEMS


def test_steps_follow_live_appends():
    """Живой путь дописывает модель запись за записью — границы шагов те
    же, что у построения целиком (номер шага — устойчивый индекс окна)."""
    from gc_tui.services.history_model import HistoryModel, _add_record

    records = _session()
    built = _model(records)
    live = HistoryModel()
    live.start_segment()
    for rec in records:
        _add_record(live, rec, seg_in_window=True)
    assert live.step_starts == built.step_starts


# ---- отрисовщик: по шагам — как целиком ------------------------------------


class _Feed(App):
    def compose(self):
        yield VerticalScroll(id="feed")


def _ev(no, kind, summary="", **kw):
    from gc_tui.models import TraceEvent

    return TraceEvent(
        no=no,
        event_ts_utc_iso="",
        kind=kind,
        title=kind,
        summary=summary,
        full_payload={},
        **kw,
    )


def _split_events():
    """Ход, где исход вызова лёг в следующий шаг (вызов, проза, исход), а
    проза, повторившая прошлый ответ хода, границы не даёт."""
    evs = [_ev(1, "user", "запрос", message_id="u1")]
    no = 2
    for k in range(8):
        evs.append(_ev(no, "agent", f"проза {k}"))
        evs.append(
            _ev(no + 1, "call", call_id=f"c{k}", tool_name="exec_command")
        )
        evs.append(_ev(no + 2, "result", "ok", call_id=f"c{k}"))
        no += 3
    evs.append(_ev(no, "call", call_id="late", tool_name="exec_command"))
    evs.append(_ev(no + 1, "agent", "проза 7"))  # повтор — не граница
    no += 2
    for k in range(4):
        evs.append(_ev(no, "call", call_id=f"d{k}", tool_name="exec_command"))
        evs.append(_ev(no + 1, "result", "ok", call_id=f"d{k}"))
        no += 2
    evs.append(_ev(no, "agent", "дальше"))
    evs.append(_ev(no + 1, "result", "поздний", call_id="late"))
    evs.append(_ev(no + 2, "agent", "итог"))
    return evs


@pytest.mark.asyncio
async def test_steps_rendered_one_by_one_match_whole_turn():
    """Шаги куском сверху по одному (как подъём) и весь ход разом —
    одинаковая лента: исход из следующего шага — под своей строкой,
    дедуп прозы — по проигранному началу хода."""
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
        render_steps,
    )
    from trace_readonly import model_of

    model = model_of(*_split_events())
    assert model.step_count() > 1
    # Исход «late» — в следующем шаге после своего вызова.
    call, result = model.call_item("late"), model.first_result("late")
    assert model.item_step(result) == model.item_step(call) + 1
    async with _Feed().run_test(size=(120, 60)) as pilot:
        feed = pilot.app.query_one("#feed", VerticalScroll)
        render_steps(
            FeedHost(feed),
            model,
            range(model.step_count()),
            before=None,
            options=ChunkOptions(),
        )
        await pilot.pause()
        whole = feed_dump(feed)
        await feed.remove_children()
        top = None
        for s in reversed(range(model.step_count())):
            refs = render_steps(
                FeedHost(feed),
                model,
                range(s, s + 1),
                before=top,
                options=ChunkOptions(),
            )
            await pilot.pause()
            top = refs.step_heads.get(s, top)
        assert feed_dump(feed) == whole


# ---- окно: подъём от живого хода -------------------------------------------


def _trace(scr):
    return scr.query_one("#trace")


def _row_y(scr, row) -> float | None:
    """Строка ``row`` в окне ленты по последней раскладке — то, что якорь
    держит и что ляжет в следующий кадр (экранный регион до кадра
    устаревший, а хром над лентой может сдвинуть её саму)."""
    if row is None:
        return None
    trace = _trace(scr)
    top = trace.placed_top(row)
    return None if top is None else top - trace.scroll_y


async def _settled(pilot, scr) -> None:
    for _ in range(3):
        await wait_until(pilot, lambda: not scr._window_busy, "встал")
        await pilot.pause(0.05)


@pytest.mark.asyncio
async def test_reading_up_giant_turns_mounts_bounded_steps(tmp_path):
    """Подъём колесом от живого хода к началу сессии: каждая подгрузка —
    не больше ``CHUNK_MAX`` виджетов, читаемая строка не прыгает, к
    началу сессии окно доходит."""
    records = _session()
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        fixture = HistoryFixture(
            records=tuple(records), segments=(tuple(records),), sidecar=()
        )
        write_history(str(tmp_path / "t"), fixture)
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        model = scr._window_model()
        assert model.step_count() > model.turn_count()
        assert scr._window_first > 0

        chunks: list[int] = []
        held: list[tuple] = []
        insert, settle = scr._window_insert, scr._window_settle

        def spy_insert(steps, *, before):
            row = _trace(scr).first_visible()
            held.append((row, _row_y(scr, row)))
            return insert(steps, before=before)

        def spy_settle(gen, widgets, side, focus):
            if side == "top":
                chunks.append(
                    sum(len(w.walk_children(with_self=True)) for w in widgets)
                )
            settle(gen, widgets, side, focus)

        scr._window_insert = spy_insert
        scr._window_settle = spy_settle
        trace = _trace(scr)
        for _ in range(3000):
            if scr._window_first == 0:
                break
            trace.post_message(
                events.MouseScrollUp(trace, 5, 5, 0, 0, 0, False, False, False)
            )
            await pilot.pause()
            if scr._window_busy or held:
                await _settled(pilot, scr)
                await pilot.pause()
            for row, y in held:
                if row is not None and row.is_attached:
                    assert _row_y(scr, row) == y
            held.clear()
        assert scr._window_first == 0
        assert chunks
        assert max(chunks) <= policy.CHUNK_MAX


def _window_consistent(scr) -> None:
    """Окно согласовано: у каждого смонтированного шага (от первого до
    конца модели, кроме разрыва) — своя голова, головы — по порядку
    шагов в ленте."""
    model = scr._window_model()
    gap = scr._window_gap
    expected = [
        s for s in range(scr._window_first, model.step_count()) if s not in gap
    ]
    heads = scr._window_heads
    assert sorted(heads) == expected
    children = list(_trace(scr).children)
    order = [children.index(heads[s]) for s in expected]
    assert order == sorted(order)


def _prose_seen(scr, turn: int) -> list[int]:
    """Номера прозы хода ``turn`` в ленте по порядку."""
    pattern = re.compile(rf"Ход {turn}: смотрю шаг (\d+)\.")
    return [
        int(m.group(1))
        for _kind, text, _classes in feed_dump(_trace(scr))
        for m in pattern.finditer(text)
    ]


@pytest.mark.asyncio
async def test_open_live_turn_steps_survive_next_turn(tmp_path, monkeypatch):
    """Последний ход без итога (агент шёл, TUI убит) открытие рисует живым
    путём — у каждого его шага своя голова. Реплика: ход перестал быть
    последним, лента у низа растёт и снимает его шаги сверху — ни один
    шаг не теряется и не застревает, подъём к началу возвращает их все
    по порядку."""
    calls = 60
    records = [*_giant_turn(0, 3), *_giant_turn(1, calls, outcome=False)]
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        fixture = HistoryFixture(
            records=tuple(records), segments=(tuple(records),), sidecar=()
        )
        write_history(str(tmp_path / "t"), fixture)
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        model = scr._window_model()
        assert model.open_turn() == 1
        assert model.step_count() - model.turn_step(1) >= 2
        _window_consistent(scr)

        monkeypatch.setattr(policy, "TAIL_MIN", 60)
        monkeypatch.setattr(policy, "CHUNK_MIN", 40)
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 160)
        for i in range(2, 5):
            feed_live(scr, _giant_turn(i, 30), splits=())
            await pilot.pause()
            await _settled(pilot, scr)
            _window_consistent(scr)
        assert scr._window_first > model.turn_step(2)

        # Подъём к началу: проза хода в ленте всякий раз — подряд идущий
        # кусок, за подъём встаёт вся.
        prose = [*range(0, calls, PROSE_EVERY), calls]
        trace = _trace(scr)
        seen: set[int] = set()
        for _ in range(400):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
            _window_consistent(scr)
            now = _prose_seen(scr, 1)
            if now:
                start = prose.index(now[0])
                assert now == prose[start : start + len(now)]
            seen.update(now)
        assert scr._window_first == 0
        assert sorted(seen) == prose


@pytest.mark.asyncio
async def test_return_to_bottom_does_not_remount_giant_turn(
    tmp_path, monkeypatch
):
    """Гигантский ход над коротким живым: приход к низу ужимает окно
    сверху, но не ниже запаса ленты — иначе догрузка тут же домонтирует
    снятое обратно (цикл «снять ↔ домонтировать» на каждый приход)."""
    records = [*_giant_turn(0, CALLS), *_giant_turn(1, 3, outcome=False)]
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        fixture = HistoryFixture(
            records=tuple(records), segments=(tuple(records),), sidecar=()
        )
        write_history(str(tmp_path / "t"), fixture)
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        trace = _trace(scr)
        # Подняться на несколько экранов: сверху домонтированы шаги.
        for _ in range(60):
            trace.post_message(
                events.MouseScrollUp(trace, 5, 5, 0, 0, 0, False, False, False)
            )
            await pilot.pause()
            await _settled(pilot, scr)
        assert scr._stick_bottom is False
        # Лимит меньше смонтированного: у низа снимать есть что.
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 40)
        forgotten, mounted = [], []
        forget, prepend = scr._window_forget_step, scr._window_prepend

        def counted_forget(step):
            done = forget(step)
            if done:
                forgotten.append(step)
            return done

        def counted_prepend(**kw):
            done = prepend(**kw)
            if done:
                mounted.append(scr._window_first)
            return done

        monkeypatch.setattr(scr, "_window_forget_step", counted_forget)
        monkeypatch.setattr(scr, "_window_prepend", counted_prepend)
        for _ in range(400):
            if scr._stick_bottom:
                break
            trace.post_message(
                events.MouseScrollDown(
                    trace, 5, 5, 0, 0, 0, False, False, False
                )
            )
            await pilot.pause()
            await _settled(pilot, scr)
        assert scr._stick_bottom is True
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        # Ужато, но не перемонтировано, и лента не ниже запаса.
        assert forgotten
        assert mounted == []
        assert not policy.underfilled(
            trace.max_scroll_y, trace.container_size.height
        )
