"""Шаг хода — единица окна ленты (спека шага 2 окна ленты, §5.3).

В реальной работе агента ход один и огромный: сотня вызовов тулов. Окно,
у которого минимальная единица — ход, монтировало такой ход одним куском
(полторы тысячи виджетов, полторы секунды без отклика). Ход делится на
шаги по границам вызовов (`trace_steps`), окно домонтирует и снимает шаги:
за одну подгрузку — не больше ``CHUNK_MAX`` виджетов, читаемая строка на
месте. Ход, нарисованный по шагам в любом порядке, выглядит так же, как
целиком."""

from __future__ import annotations

import json
import re

import pytest
from feed_dump import (
    HISTORY_THREAD,
    HistoryFixture,
    feed_dump,
    feed_live,
    without_ephemeral,
    write_history,
)
from gc_testkit.tui_support import pinned_snapshot_clock, wait_until
from gc_tui.services import trace_window_policy as policy
from gc_tui.services.trace_steps import (
    SPLIT_FROM_ITEMS,
    STEP_MAX_ITEMS,
    STEP_MIN_ITEMS,
)
from test_feed_parity_baseline import _settled_dump
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


def _find(i: int, k: int) -> list[dict]:
    """Поиск по файлам — вызов группы `Explored`."""
    cid = f"find_{i}_{k}"
    args = {"path": "/p", "pattern": f"f{k}"}
    output = {"tool": "fs_find_in_files", "ok": True, **args, "matches": []}
    return [
        _message(
            {
                "arguments": json.dumps(args),
                "call_id": cid,
                "name": "fs_find_in_files",
                "type": "function_call",
                "id": f"fc_{cid}",
                "status": "completed",
            }
        ),
        _message(
            {
                "id": f"fco_{cid}",
                "type": "function_call_output",
                "call_id": cid,
                "output": json.dumps(output),
            }
        ),
    ]


def _calls_turn(i: int, calls: int) -> list[dict]:
    """Ход без прозы: реплика, ``calls`` вызовов подряд (поиски одной
    группой, команды — каждая своим блоком, вперемежку сериями) и
    ответ с итогом."""
    user = f"m_u{i:04d}"
    records = [
        _message({"role": "user", "id": user, "content": f"запрос {i}"})
    ]
    for k in range(calls):
        records += _find(i, k) if k % 40 < 30 else _call(i, k)
    answer = _prose(i, calls)
    return [*records, answer, _outcome(user, answer["payload"]["id"])]


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


def test_calls_without_prose_split_at_call_boundaries():
    """Сотня вызовов подряд без прозы — шаги не больше ``STEP_MAX_ITEMS``
    (по оценке — не больше куска окна), строка вызова и её исход — в
    одном шаге."""
    calls = 120
    model = _model(_calls_turn(0, calls))
    assert model.step_count() > 1
    for s in range(model.step_count()):
        span = model.step_range(s)
        assert len(span) <= STEP_MAX_ITEMS
        assert len(span) * policy.ITEM_WIDGETS <= policy.CHUNK_MAX
    for k in range(calls):
        cid = f"find_0_{k}" if k % 40 < 30 else f"call_0_{k}"
        result = model.first_result(cid)
        assert model.item_step(result) == model.item_step(model.call_item(cid))


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


@pytest.mark.asyncio
async def test_calls_without_prose_open_matches_live(tmp_path, monkeypatch):
    """Шаги серии вызовов без прозы режут группу поисков, шаги хода с
    прозой встают на прозе: кусками при открытии и живым путём лента одна
    и та же — группа закрывается на границе шага на обоих путях, дедуп
    ответа — одним правилом."""
    monkeypatch.setattr(policy, "TAIL_MIN", 10**6)
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 10**7)
    records = [
        *_calls_turn(0, 120),
        *_giant_turn(1, 40),
        *_giant_turn(2, 3),
    ]
    assert _model(records).step_count() > 3
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "open").run_test(size=_SIZE) as pilot:
            write_history(str(tmp_path / "open"), fixture)
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
            opened = await _settled_dump(pilot)
        async with _app(tmp_path / "live").run_test(size=_SIZE) as pilot:
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD)
            feed_live(scr, records, splits=())
            live = await _settled_dump(pilot)
    assert without_ephemeral(live) == opened


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


def _small_window(monkeypatch) -> int:
    """Окно в десяток шагов (инвариант лимит ≥ хвост + 2 куска; лимит
    больше удерживаемого при чтении — видимого с двумя запасами по
    строке на виджет); возвращает «лимит + кусок»."""
    monkeypatch.setattr(policy, "TAIL_MIN", 80)
    monkeypatch.setattr(policy, "CHUNK_MIN", 50)
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 300)
    return policy.WINDOW_LIMIT + policy.CHUNK_MIN


def _unpinned_widgets(scr) -> int:
    """Виджетов поддерева ленты в незакреплённых шагах окна."""
    steps, counts = scr._window_mounted_counts()
    return sum(
        c
        for s, c in zip(steps, counts, strict=True)
        if not scr._window_pinned(s)
    )


async def _feed_turn(pilot, scr, records, *, bound=None) -> None:
    """Записи живым путём пачками по десятку; ``bound`` — после каждой
    пачки у низа незакреплённое в его пределах."""
    for start in range(0, len(records), 10):
        feed_live(scr, records[start : start + 10], splits=())
        await pilot.pause()
        await _settled(pilot, scr)
        _window_consistent(scr)
        if bound is not None:
            assert scr._stick_bottom
            assert _unpinned_widgets(scr) <= bound


@pytest.mark.asyncio
async def test_live_giant_turn_at_bottom_stays_bounded(tmp_path, monkeypatch):
    """Живой гигантский ход у низа: закреплён только его последний шаг —
    старые шаги снимаются сверху, окно в пределах «лимит + кусок»."""
    bound = _small_window(monkeypatch)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(
            pilot, scr, _giant_turn(0, 180, outcome=False), bound=bound
        )
        model = scr._window_model()
        assert model.open_turn() == 0
        assert scr._window_first > 0
        pinned = [s for s in scr._window_heads if scr._window_pinned(s)]
        assert pinned == [model.step_count() - 1]


@pytest.mark.asyncio
async def test_running_call_in_old_live_step_stays_mounted(
    tmp_path, monkeypatch
):
    """Идущий вызов в старом шаге живого хода — живое состояние: его шаг
    закреплён и остаётся в ленте, пока ход растёт у низа."""
    _small_window(monkeypatch)
    records = _giant_turn(0, 120, outcome=False)
    running = "call_0_4"
    records = [
        r for r in records if r["payload"].get("id") != f"fco_{running}"
    ]
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        # Ход идёт: тик не обрывает вызов без исхода.
        monkeypatch.setattr(scr, "_task_status_badge", lambda: "[running]")
        await pilot.pause()
        await _feed_turn(pilot, scr, records)
        row = scr._active_calls[running]
        assert row.parent is _trace(scr)
        model = scr._window_model()
        step = model.item_step(model.call_item(running))
        assert step < model.step_count() - 1
        assert scr._window_pinned(step)
        assert step in scr._window_heads


@pytest.mark.asyncio
async def test_reading_up_trims_old_steps_of_live_turn(tmp_path, monkeypatch):
    """Подъём по живому гигантскому ходу: снизу снимаются его старые
    шаги — разрыв внутри хода, последний шаг на месте."""
    bound = _small_window(monkeypatch)
    records = _giant_turn(0, 180, outcome=False)
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
        last = model.step_count() - 1
        # Открытие у низа уже сняло старые шаги живого хода.
        assert scr._window_first > 0
        trace = _trace(scr)
        for _ in range(200):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
            _window_consistent(scr)
            assert _unpinned_widgets(scr) <= bound
        assert scr._window_first == 0
        assert scr._window_gap
        assert scr._window_gap.stop <= last
        assert last in scr._window_heads


@pytest.mark.asyncio
async def test_turn_end_reaches_returned_steps_of_live_turn(
    tmp_path, monkeypatch
):
    """Старый шаг живого хода снят у низа и вернулся куском при подъёме;
    итог хода доходит и до него — время вызова застыло, как у шагов,
    нарисованных живым путём."""
    from gc_tui.widgets.tool_call_row import ToolCallRow

    _small_window(monkeypatch)
    calls = 180
    records = _giant_turn(0, calls, outcome=False)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, records)
        assert scr._window_first > 0
        trace = _trace(scr)
        for _ in range(200):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        assert scr._window_first == 0
        outcome = _outcome("m_u0000", f"msg_p00_{calls:03d}")
        outcome["outcome"]["calls"] = {"call_0_0": 7.0}
        feed_live(scr, [outcome], splits=())
        await pilot.pause()
        rows = [r for r in trace.query(ToolCallRow) if r.call_id == "call_0_0"]
        assert len(rows) == 1
        assert rows[0]._frozen == 7.0


@pytest.mark.asyncio
async def test_forgotten_step_leaves_registries_neighbour_stays(tmp_path):
    """Шаг многошагового хода снят (`forget_step`): ни один реестр экрана
    не держит его виджеты, вида блоков его шага нет — а у соседнего шага
    того же хода вид и виджеты на месте."""
    from gc_tui.services.history_render import history_level
    from test_forget_turn import _held

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
        model = scr._window_model()
        step = scr._window_first
        assert model.step_turn(step) == model.step_turn(step + 1) == 0
        gone = {
            w
            for top in scr._window_step_widgets(step)
            for w in top.walk_children(with_self=True)
        }
        kept = set(scr._window_step_widgets(step + 1))
        assert history_level(step) in scr._history_views
        assert scr.forget_step(step)
        await pilot.pause()
        assert not gone & set(_held(scr))
        assert history_level(step) not in scr._history_views
        assert history_level(step + 1) in scr._history_views
        assert all(w.is_attached for w in kept)
        _window_consistent(scr)


@pytest.mark.asyncio
async def test_ephemeral_row_returns_with_its_step(tmp_path, monkeypatch):
    """Эфемерная строка (без события модели) посреди живого гигантского
    хода снимается у низа вместе со своим шагом и возвращается с ним при
    подъёме — на своём месте, эфемерной."""
    from gc_tui.services.trace_ephemera import EPHEMERAL_CLASS

    _small_window(monkeypatch)
    records = _giant_turn(0, 180, outcome=False)
    at = next(
        i
        for i, rec in enumerate(records)
        if rec["payload"].get("id") == "msg_p00_009"
    )
    note = "эфемерная строка шага"
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        feed_live(scr, records[: at + 1], splits=())
        scr._mount_trace_row(prefix="", content=note, kind_class="line-meta")
        await _feed_turn(pilot, scr, records[at + 1 :])
        trace = _trace(scr)
        assert not any(note in t for _k, t, _c in feed_dump(trace))
        for _ in range(200):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        assert scr._window_first == 0
        rows = feed_dump(trace)
        prose = next(
            i for i, row in enumerate(rows) if "смотрю шаг 9." in row[1]
        )
        assert [r[1] for r in rows].count(note) == 1
        assert rows[prose + 1][1] == note
        assert EPHEMERAL_CLASS in rows[prose + 1][2]


@pytest.mark.asyncio
async def test_climb_over_giant_last_turn_stays_bounded(tmp_path, monkeypatch):
    """Последний ход сессии — законченный гигантский (стенд, снимок G):
    подъём к началу держит всё смонтированное, вместе с закреплённым, в
    пределах «лимит + кусок» и последнего шага — ход не закреплён
    целиком."""
    bound = _small_window(monkeypatch)
    records = [*_giant_turn(0, 3), *_giant_turn(1, 180)]
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
        last = model.step_count() - 1
        trace = _trace(scr)
        for _ in range(200):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
            steps, counts = scr._window_mounted_counts()
            pinned = dict(zip(steps, counts, strict=True)).get(last, 0)
            assert sum(counts) <= bound + pinned
        assert scr._window_first == 0


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
        # Подняться на несколько экранов: сверху домонтированы шаги. Два
        # щелчка за раз: один щелчок (две строки) не выходит из допуска
        # липкости, и тик опроса возвращает ленту к низу.
        for _ in range(60):
            for _click in range(2):
                trace.post_message(
                    events.MouseScrollUp(
                        trace, 5, 5, 0, 0, 0, False, False, False
                    )
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
