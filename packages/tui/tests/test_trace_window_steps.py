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
    RESULT_KINDS,
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


def _call_and_result(model, call_id: str) -> tuple[int, int | None]:
    """Индексы последнего вызова ``call_id`` и его первого исхода."""
    call = model.call_before(call_id, len(model.items))
    assert call is not None
    return call, model.result_of(call)


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
        call, result = _call_and_result(model, cid)
        assert model.item_step(result) == model.item_step(call)


def test_parallel_batch_bounds_step_by_one_batch():
    """Пачку параллельных вызовов граница не режет — вызовы ждут исходов:
    шаг больше ``STEP_MAX_ITEMS`` ровно на пачку (R43), а вызов и исход
    — в одном шаге."""
    from gc_tui.services.history_model import HistoryModel

    width = 10
    evs = [_ev(1, "user", "запрос", message_id="u1")]
    # Шаг из ``STEP_MAX_ITEMS - 2`` элементов: пачка начинается до
    # обязательной границы.
    for k in range(STEP_MAX_ITEMS - 3):
        evs.append(_ev(len(evs) + 1, "agent", f"проза {k}"))
    for k in range(width):
        evs.append(_ev(len(evs) + 1, "call", call_id=f"p{k}"))
    for k in range(width):
        evs.append(_ev(len(evs) + 1, "result", "ok", call_id=f"p{k}"))
    evs.append(_ev(len(evs) + 1, "agent", "дальше"))
    model = HistoryModel(turn_starts=[0])
    for ev in evs:
        model.append_event(ev)
    first = model.step_range(0)
    assert len(first) == STEP_MAX_ITEMS - 2 + 2 * width
    assert model.item_step(len(evs) - 1) == 1


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
    from gc_tui.screens.trace_window import step_heads
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
        render_steps,
    )
    from trace_readonly import model_of

    model = model_of(*_split_events())
    assert model.step_count() > 1
    # Исход «late» — в следующем шаге после своего вызова.
    call, result = _call_and_result(model, "late")
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
            top = step_heads(feed.children, refs.step_of).get(s, top)
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
    шагов в ленте, и голова — первый по ленте виджет своего шага: каждое
    событие модели лежит в ленте между головой своего шага и следующей
    (исход — в шаге своего вызова), заголовок группы — в шаге своих
    строк."""
    model = scr._window_model()
    gap = scr._window_gap
    expected = [
        s
        for s in range(scr._window_first, model.step_count())
        if s not in gap and _drawable(model, s)
    ]
    heads = scr._window_heads
    assert sorted(heads) == expected
    children = list(_trace(scr).children)
    order = [children.index(heads[s]) for s in expected]
    assert order == sorted(order)
    _events_in_their_steps(scr, children)


def _drawable(model, s: int) -> bool:
    """У шага ``s`` есть что рисовать: показанное событие (не свёрнутая
    попытка сдачи) или черта смены модели. Шаг откаченной реплики без
    вывода и шаг, где осталась только свёрнутая попытка, — пустые."""
    from gc_tui.services.trace_steps import shown

    def drawn(i: int) -> bool:
        item = model.items[i]
        if item.kind == "model_choice":
            return True
        ev = item.event
        return shown(ev, model.withdrawn) and (
            ev.no not in model.collapsed_attempts
        )

    return any(drawn(i) for i in model.step_range(s))


def _event_step(model, i: int) -> int:
    """Шаг, в котором отрисовщик рисует элемент ``i``: исход — в шаге
    своего вызова."""
    ev = model.items[i].event
    call = model.call_of(i) if ev.kind in RESULT_KINDS else None
    return model.item_step(i if call is None else call)


def _events_in_their_steps(scr, children: list) -> None:
    """Виджеты событий — в шаге своего события по модели, заголовок группы
    — в шаге первой строки за ним."""
    from gc_tui.textual_compat import is_being_removed
    from gc_tui.widgets.tool_call_row import ToolCallRow

    model = scr._window_model()
    item_of = {
        it.event.no: i
        for i, it in enumerate(model.items)
        if it.kind == "event" and it.event is not None
    }
    step_at = {w: s for s, w in scr._window_heads.items()}
    step = header = None
    for widget in children:
        step = step_at.get(widget, step)
        if is_being_removed(widget):
            continue
        i = item_of.get(getattr(widget, "_event_no", 0) or 0)
        if i is not None:
            owner = _event_step(model, i)
            assert step == owner
            assert header in (None, owner)
            header = None
        if isinstance(widget, ToolCallRow) and widget._mode == "header":
            header = step


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


async def _feed_turn(
    pilot, scr, records, *, bound=None, whole_calls=False
) -> None:
    """Записи живым путём пачками по десятку; ``bound`` — после каждой
    пачки у низа незакреплённое в его пределах; ``whole_calls`` — пачка
    не кончается между вызовом и его исходом (иначе тик между пачками
    обрывает вызов «прервано», а открытие рисует его законченным)."""
    for batch in _batches(records, 10, whole_calls=whole_calls):
        feed_live(scr, batch, splits=())
        await pilot.pause()
        await _settled(pilot, scr)
        _window_consistent(scr)
        if bound is not None:
            assert scr._stick_bottom
            assert _unpinned_widgets(scr) <= bound


def _batches(records, size: int, *, whole_calls: bool) -> list[list[dict]]:
    """Записи пачками по ``size``; ``whole_calls`` — исход вызова в пачке
    своего вызова."""
    out: list[list[dict]] = []
    for rec in records:
        output = rec.get("payload", {}).get("type") == "function_call_output"
        if out and (len(out[-1]) < size or (whole_calls and output)):
            out[-1].append(rec)
        else:
            out.append([rec])
    return out


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
        step = model.item_step(_call_and_result(model, running)[0])
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


# ---- головы шагов живого пути — по ленте -----------------------------------


def _finish(i: int, n: int, *, ok: bool) -> list[dict]:
    """Попытка сдачи хода ``i`` номер ``n``: вызов `turn_finish` с call_id
    и его исход (принята или отклонена судьёй финала)."""
    cid = f"fin_{i}_{n}"
    args = {
        "solution_type": "full_solution",
        "task": f"ход {i}",
        "handoff": "{}",
        "task_report": f"Отчёт {i} попытка {n}.",
    }
    output = {
        "tool": "turn_finish",
        "ok": ok,
        "status": "submitted" if ok else "rejected_by_finish_gate",
    }
    return [
        _message(
            {
                "arguments": json.dumps(args, ensure_ascii=False),
                "call_id": cid,
                "name": "turn_finish",
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


def _refused_finish_turn(i: int, after: int) -> list[dict]:
    """Ход: первая сдача — первое, что рисует шаг (шаг начат на 22-м
    элементе прозой, которую гасит дедуп ответа: она повторяет прошлую),
    судья её отклоняет, потом ``after`` вызовов и вторая, принятая
    сдача."""
    user = f"m_u{i:04d}"
    records = [
        _message({"role": "user", "id": user, "content": f"запрос {i}"}),
        _prose(i, 0),
    ]
    for k in range(9):
        records += _call(i, k)
    again = _prose(i, 1)
    again["payload"] = {**again["payload"], "id": f"msg_again_{i}"}
    records += [_prose(i, 1), again, *_finish(i, 1, ok=False)]
    for k in range(10, 10 + after):
        records += _call(i, k)
    records += _finish(i, 2, ok=True)
    return [*records, _outcome(user, f"fco_fin_{i}_2")]


async def _opened_dump(tmp_path, records, monkeypatch) -> list:
    """Лента открытия сессии целиком (хвост — вся история)."""
    with monkeypatch.context() as m:
        m.setattr(policy, "TAIL_MIN", 10**6)
        m.setattr(policy, "WINDOW_LIMIT", 10**7)
        async with _app(tmp_path).run_test(size=_SIZE) as pilot:
            write_history(
                str(tmp_path),
                HistoryFixture(
                    records=tuple(records),
                    segments=(tuple(records),),
                    sidecar=(),
                ),
            )
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
            return await _settled_dump(pilot)


async def _climb_whole(pilot, scr, monkeypatch) -> list:
    """Подъём к началу без снятия (лимит снят): вся история в ленте —
    её дамп без эфемерного."""
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 10**7)
    trace = _trace(scr)
    for _ in range(400):
        if scr._window_first == 0:
            break
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        await _settled(pilot, scr)
        _window_consistent(scr)
    assert scr._window_first == 0
    return without_ephemeral(await _settled_dump(pilot))


@pytest.mark.asyncio
async def test_refused_finish_head_keeps_turn_answer(tmp_path, monkeypatch):
    """Первая сдача хода — первый нарисованный виджет шага, и судья её
    отклонил: свёртка попыток снимает её виджеты мимо окна. Голова шага
    переходит к следующему его виджету — вызовы после отказа и принятый
    отчёт остаются в своём шаге: снятие сверху следующими ходами и
    подъём не теряют ответ хода."""
    records = _refused_finish_turn(0, 30)
    model = _model(records)
    item = {
        it.event.message_id: i
        for i, it in enumerate(model.items)
        if it.event is not None
    }
    again, first = item["msg_again_0"], item["fc_fin_0_1"]
    assert model.starts_step(model.items[again].event.no)
    assert model.item_step(first) == model.item_step(again)
    later = [*_giant_turn(1, 30), *_giant_turn(2, 30)]
    with pinned_snapshot_clock(monkeypatch):
        opened = await _opened_dump(
            tmp_path / "open", [*records, *later], monkeypatch
        )
        _small_window(monkeypatch)
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD)
            await pilot.pause()
            await _feed_turn(pilot, scr, records, whole_calls=True)
            await _feed_turn(pilot, scr, later, whole_calls=True)
            assert scr._window_first > 0
            live = await _climb_whole(pilot, scr, monkeypatch)
    answers = [t for _k, t, _c in live if "Отчёт 0 попытка 2." in t]
    assert len(answers) == 1
    assert live == opened


@pytest.mark.asyncio
async def test_live_calls_without_prose_header_stays_with_group(
    tmp_path, monkeypatch
):
    """Серия поисков без прозы вживую: шаг начат вызовом, заголовок
    `Explored` встаёт перед ним — он голова шага, а не хвост прошлого.
    Снятие сверху у низа уносит заголовок только со своей группой,
    подъём возвращает его один раз — лента как при открытии."""
    records = [*_calls_turn(0, 60), *_calls_turn(1, 60)]
    with pinned_snapshot_clock(monkeypatch):
        opened = await _opened_dump(tmp_path / "open", records, monkeypatch)
        _small_window(monkeypatch)
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD)
            await pilot.pause()
            await _feed_turn(pilot, scr, records, whole_calls=True)
            assert scr._window_first > 0
            live = await _climb_whole(pilot, scr, monkeypatch)
    assert live == opened


@pytest.mark.asyncio
async def test_expanded_step_of_live_turn_is_not_pinned(tmp_path):
    """Раскрытое в старом шаге текущего хода шаг не закрепляет (R41):
    снятый шаг возвращается куском раскрытым — снимок вида его хранит."""
    user = "m_u0000"
    records = [_message({"role": "user", "id": user, "content": "запрос"})]
    for k in range(30):
        records += _find(0, k)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, records, whole_calls=True)
        model = scr._window_model()
        assert model.open_turn() == 0
        assert model.step_count() > 1
        view = scr._block_views[""]
        view.toggle(view.model.block_of("find_0_0"))
        await pilot.pause()
        texts = [t for _k, t, _c in feed_dump(_trace(scr))]
        assert "    − свернуть группу" in texts
        assert not scr._step_pinned(0)
        assert scr.forget_step(0)
        await pilot.pause()
        assert "    − свернуть группу" not in [
            t for _k, t, _c in feed_dump(_trace(scr))
        ]
        refs = scr._window_render(range(1), before=scr._window_top())
        scr._window_first = 0
        scr._window_adopt_chunk(refs)
        await pilot.pause()
        texts = [t for _k, t, _c in feed_dump(_trace(scr))]
        assert texts.count("    − свернуть группу") == 1


# ---- R42: шаги и после отклонённой сдачи -----------------------------------


def _finish_nos(model) -> set[int]:
    """Номера событий сдачи хода в модели."""
    from gc_tui.services.trace_mapper import is_finish

    return {
        it.event.no
        for it in model.items
        if it.event is not None and is_finish(it.event)
    }


def _drawn_finish_nos(scr, model) -> set[int]:
    """Номера событий сдачи, чьи виджеты стоят в ленте."""
    from gc_tui.textual_compat import is_being_removed

    finishes = _finish_nos(model)
    return {
        w._event_no
        for w in _trace(scr).children
        if getattr(w, "_event_no", 0) in finishes and not is_being_removed(w)
    }


def _attempts_session() -> list[dict]:
    """Попытки сдачи всех видов: отклонённая и следующая попытка;
    отклонённая и проза, повторившая её отчёт; одна принятая."""
    user = "m_u0002"
    echo = _prose(2, 0)
    echo["payload"]["content"][0]["text"] = "Отчёт 2 попытка 1."
    return [
        *_refused_finish_turn(0, 3),
        *_refused_finish_turn(1, 0),
        _message({"role": "user", "id": user, "content": "запрос 2"}),
        *_call(2, 0),
        *_finish(2, 1, ok=False),
        echo,
        _outcome(user, echo["payload"]["id"]),
    ]


def test_refused_finish_then_long_work_splits_into_bounded_steps():
    """Отклонённая сдача и сотня вызовов после неё — шаги не больше
    ``STEP_MAX_ITEMS`` (R42); модель помечает попытку, которую свернёт
    следующая."""
    model = _model(_refused_finish_turn(0, 110))
    for s in range(model.step_count()):
        assert len(model.step_range(s)) <= STEP_MAX_ITEMS
    first = next(
        it.event.no
        for it in model.items
        if it.event is not None and it.event.call_id == "fin_0_1"
    )
    assert model.collapsed_attempts == {first}


@pytest.mark.asyncio
async def test_finish_attempt_marks_match_both_renderers(tmp_path):
    """Страж паритета: попытки, которые модель пометила свёрнутыми, —
    ровно те, что живой путь снял, а отрисовщик кусков не нарисовал;
    остальные сдачи стоят в ленте на обоих путях."""
    records = _attempts_session()
    model = _model(records)
    assert model.collapsed_attempts
    expected = _finish_nos(model) - model.collapsed_attempts
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    async with _app(tmp_path / "open").run_test(size=_SIZE) as pilot:
        write_history(str(tmp_path / "open"), fixture)
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        opened = scr._window_model()
        assert opened.collapsed_attempts == model.collapsed_attempts
        assert _drawn_finish_nos(scr, opened) == expected
    async with _app(tmp_path / "live").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, records, whole_calls=True)
        live = scr._window_model()
        assert live.collapsed_attempts == model.collapsed_attempts
        assert _drawn_finish_nos(scr, live) == expected


@pytest.mark.asyncio
async def test_refused_finish_long_work_trim_and_remount(
    tmp_path, monkeypatch
):
    """Отклонённая сдача, сотня вызовов, принятая сдача — шагами (R42).
    Следующие ходы снимают их сверху, подъём возвращает: ни потерь, ни
    повторов — лента как при открытии."""
    records = _refused_finish_turn(0, 110)
    model = _model(records)
    assert model.step_count() > 5
    later = [*_giant_turn(1, 30), *_giant_turn(2, 30)]
    with pinned_snapshot_clock(monkeypatch):
        opened = await _opened_dump(
            tmp_path / "open", [*records, *later], monkeypatch
        )
        _small_window(monkeypatch)
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = pilot.app.screen
            assert scr.attach_active_task(HISTORY_THREAD)
            await pilot.pause()
            await _feed_turn(pilot, scr, records, whole_calls=True)
            await _feed_turn(pilot, scr, later, whole_calls=True)
            # Сняты шаг отклонённой попытки и шаги работы после неё.
            assert scr._window_first > 5
            live = await _climb_whole(pilot, scr, monkeypatch)
    assert sum("Отчёт 0 попытка 2." in t for _k, t, _c in live) == 1
    assert not any("Отчёт 0 попытка 1." in t for _k, t, _c in live)
    assert live == opened


def _texts(scr) -> list[str]:
    return [t for _k, t, _c in feed_dump(_trace(scr))]


async def _climb(pilot, scr) -> None:
    """Подъём к началу истории с текущим окном."""
    trace = _trace(scr)
    for _ in range(400):
        if scr._window_first == 0:
            break
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        await _settled(pilot, scr)
    assert scr._window_first == 0


@pytest.mark.asyncio
async def test_refused_attempt_does_not_pin_live_turn(tmp_path, monkeypatch):
    """Отклонённая попытка текущего хода шаг не закрепляет (R44): сотня
    вызовов после отказа у низа держит окно в пределах, шаг попытки
    снимается. Подъём посреди хода возвращает её куском — экран берёт её
    виджеты в свёртку, и вторая попытка снимает её, где бы она ни стояла:
    ни в ленте, ни после подъёма и возврата отклонённой попытки нет."""
    bound = _small_window(monkeypatch)
    records = _refused_finish_turn(0, 110)
    second = next(
        i
        for i, rec in enumerate(records)
        if rec["payload"].get("id") == "fc_fin_0_2"
    )
    refused = "Отчёт 0 попытка 1."
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(
            pilot, scr, records[:second], bound=bound, whole_calls=True
        )
        model = scr._window_model()
        attempt = model.item_step(
            next(
                i
                for i, it in enumerate(model.items)
                if it.event is not None and it.event.call_id == "fin_0_1"
            )
        )
        assert scr._window_first > attempt
        assert not any(refused in t for t in _texts(scr))
        await _climb(pilot, scr)
        assert sum(refused in t for t in _texts(scr)) == 1
        await _feed_turn(pilot, scr, records[second:], whole_calls=True)
        assert not any(refused in t for t in _texts(scr))
        scr._scroll_to_bottom()
        await pilot.pause()
        await _settled(pilot, scr)
        await _climb(pilot, scr)
        texts = _texts(scr)
        assert not any(refused in t for t in texts)
        assert sum("Отчёт 0 попытка 2." in t for t in texts) == 1


@pytest.mark.asyncio
async def test_empty_step_does_not_stop_top_trim(tmp_path, monkeypatch):
    """Реплика записана — модель открыла её ход и шаг, — и откачена до
    вывода агента: у шага виджетов нет навсегда. Снятие сверху у низа
    проходит через него (R45): гигантские ходы после отката держат окно в
    пределах после каждой пачки."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    bound = _small_window(monkeypatch)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, _giant_turn(0, 3), whole_calls=True)
        user = "m_u0001"
        feed_live(
            scr,
            [_message({"role": "user", "id": user, "content": "отменю"})],
            splits=(),
        )
        await pilot.pause()
        model = scr._window_model()
        empty = model.step_count() - 1
        scr._withdraw_from_history(user)
        list(scr.query(UserReplyContainer))[-1].remove()
        await pilot.pause()
        assert empty not in scr._window_heads
        for i in (2, 3):
            await _feed_turn(
                pilot, scr, _giant_turn(i, 90), bound=bound, whole_calls=True
            )
        assert scr._window_first > empty


@pytest.mark.asyncio
async def test_settled_turn_line_does_not_pin_its_step(tmp_path, monkeypatch):
    """Строка итога прошлого хода осела с началом следующего — она больше
    не живое состояние: её шаг снимается сверху, гигантский следующий ход
    у низа держит окно в пределах."""
    bound = _small_window(monkeypatch)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, _giant_turn(0, 3), whole_calls=True)
        await _feed_turn(
            pilot, scr, _giant_turn(1, 90), bound=bound, whole_calls=True
        )
        assert scr._window_first > 0


@pytest.mark.asyncio
async def test_empty_step_after_gap_does_not_stop_bottom_trim(
    tmp_path, monkeypatch
):
    """Пустой шаг (откаченная реплика) встал сразу за разрывом — первым
    шагом закреплённого низа: при чтении вверх он не останавливает снятие
    снизу (R45, край ``gap.stop``). Исправленная реплика, подъём, пока шаг
    исправленной реплики последний, ход продолжается шагами — подъём к
    началу держит окно в пределах после каждого куска."""
    from gc_tui.widgets.user_reply import UserReplyContainer

    bound = _small_window(monkeypatch)
    later = _giant_turn(2, 90, outcome=False)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, _giant_turn(0, 90), whole_calls=True)
        user = "m_u0001"
        feed_live(
            scr,
            [_message({"role": "user", "id": user, "content": "отменю"})],
            splits=(),
        )
        await pilot.pause()
        model = scr._window_model()
        empty = model.step_count() - 1
        scr._withdraw_from_history(user)
        list(scr.query(UserReplyContainer))[-1].remove()
        feed_live(scr, later[:3], splits=())
        await pilot.pause()
        await _settled(pilot, scr)
        assert model.step_count() - 1 == empty + 1
        trace = _trace(scr)

        # Разрыв дошёл до пустого шага: он на краю закреплённого низа.
        def at_gap() -> bool:
            gap = scr._window_gap
            return bool(gap) and gap.start <= empty <= gap.stop

        for _ in range(50):
            if at_gap():
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        assert at_gap()
        for batch in _batches(later[3:], 10, whole_calls=True):
            feed_live(scr, batch, splits=())
            await pilot.pause()
        await _settled(pilot, scr)
        assert model.step_count() - 1 > empty + 2
        for _ in range(400):
            if scr._window_first == 0:
                break
            trace.scroll_to(y=0, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
            assert _unpinned_widgets(scr) <= bound
        assert scr._window_first == 0


# ---- липкая реплика при чтении гигантского хода (R46) ----------------------


async def _open_giant(pilot, tmp_path, records):
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    write_history(str(tmp_path), fixture)
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
    await pilot.pause(0.3)
    await _settled(pilot, scr)
    return scr


async def _scroll_to(pilot, scr, y: int):
    _trace(scr).scroll_to(y=y, animate=False)
    await pilot.pause()
    await _settled(pilot, scr)
    scr._refresh_trace_overlays()
    await pilot.pause()


def _sticky(scr):
    from gc_tui.widgets.trace_overlays import StickyReply

    return scr.query_one("#trace-sticky-reply", StickyReply)


def _sticky_text(scr) -> str | None:
    sticky = _sticky(scr)
    if not sticky.display or sticky.reply is None:
        return None
    return sticky.reply.quiet_line().plain


@pytest.mark.asyncio
async def test_sticky_reply_of_giant_turn_read_deep(tmp_path, monkeypatch):
    """Чтение посреди гигантского хода, чей первый шаг снят окном: липкая
    реплика — реплика этого хода из модели, тем же видом (R46). Клик —
    шаги до реплики домонтируются, реплика наверху. Ниже, в следующем
    ходе, — его реплика; у низа липкой реплики нет."""
    from gc_tui.messages import ScrollToReply
    from gc_tui.widgets.user_reply import UserReplyContainer

    _small_window(monkeypatch)
    records = [*_giant_turn(0, 60), *_giant_turn(1, 90), *_giant_turn(2, 60)]
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_giant(pilot, tmp_path / "t", records)
        model = scr._window_model()
        trace = _trace(scr)
        assert _sticky_text(scr) is None
        # Читать вверх по ходу 2, пока его первый шаг не снят и чтение не
        # ушло в ход 1 глубже его снятого начала.
        for _ in range(200):
            await _scroll_to(pilot, scr, max(0, trace.scroll_y - 20))
            row = scr._window_reading_row()
            step = scr._window_step_at(row)
            if (
                step is not None
                and model.step_turn(step) == 1
                and scr._window_first > model.turn_step(1)
            ):
                break
        else:
            raise AssertionError("не дошли до глубины хода 1")
        assert scr._stick_bottom is False
        assert _sticky_text(scr) == "› запрос 1"
        sticky = _sticky(scr)
        assert not sticky.reply.is_attached  # реплика — из модели
        scr.post_message(ScrollToReply(sticky.reply))
        await pilot.pause()
        await _settled(pilot, scr)
        await pilot.pause()
        head = scr._window_heads.get(model.turn_step(1))
        assert isinstance(head, UserReplyContainer)
        assert head.plain_text == "запрос 1"
        # Верх реплики (с её отступом) — первая строка видимой области.
        top = round(trace.scroll_y) + head.styles.margin.top
        assert trace.placed_top(head) == top
        # Вниз, в ход 2 глубже его реплики.
        start2 = model.turn_step(2)
        for _ in range(400):
            await _scroll_to(pilot, scr, trace.scroll_y + 20)
            step = scr._window_step_at(scr._window_reading_row())
            if step is not None and step > start2:
                break
        assert _sticky_text(scr) == "› запрос 2"
        scr._scroll_to_bottom()
        await pilot.pause()
        await _settled(pilot, scr)
        scr._refresh_trace_overlays()
        assert _sticky_text(scr) is None


def _reused_ids_model():
    """Два хода с одними и теми же call_id (бэкенд нумерует вызовы
    заново в каждом ходе): во втором ходе исход «late» лёг в следующий
    шаг после своего вызова."""
    from dataclasses import replace

    from gc_tui.services.history_model import HistoryModel

    first = _split_events()
    second = [replace(ev, no=ev.no + 100) for ev in first]
    second[0] = replace(second[0], summary="запрос 2", message_id="u2")
    model = HistoryModel()
    model.turn_starts.append(0)
    for ev in first:
        model.append_event(ev)
    model.turn_starts.append(len(model.items))
    for ev in second:
        model.append_event(ev)
    return model


@pytest.mark.asyncio
async def test_reused_call_ids_across_turns_render_by_steps():
    """call_id повторён в позднем ходе: исход принадлежит ближайшему
    вызову выше с тем же id. Шаги второго хода по одному (как подъём) —
    та же лента, что весь ход разом: исход не сирота и не у чужого
    вызова."""
    from gc_tui.screens.trace_window import step_heads
    from gc_tui.services.history_render import (
        ChunkOptions,
        FeedHost,
        render_steps,
    )

    model = _reused_ids_model()
    second = range(model.turn_step(1), model.step_count())
    async with _Feed().run_test(size=(120, 60)) as pilot:
        feed = pilot.app.query_one("#feed", VerticalScroll)
        render_steps(
            FeedHost(feed), model, second, before=None, options=ChunkOptions()
        )
        await pilot.pause()
        whole = feed_dump(feed)
        await feed.remove_children()
        top = None
        for s in reversed(second):
            refs = render_steps(
                FeedHost(feed),
                model,
                range(s, s + 1),
                before=top,
                options=ChunkOptions(),
            )
            await pilot.pause()
            top = step_heads(feed.children, refs.step_of).get(s, top)
        assert feed_dump(feed) == whole


@pytest.mark.asyncio
async def test_live_collapse_follows_model_marks(tmp_path):
    """Живой путь снимает попытки по пометкам модели — один источник
    свёртки (R47): даже если дедуп ответа экрана начат заново посреди
    хода, вторая попытка снимает первую."""
    from gc_tui.services.answer_flow import AnswerFlow

    records = _refused_finish_turn(0, 2)
    second = next(
        i
        for i, rec in enumerate(records)
        if rec["payload"].get("id") == "fc_fin_0_2"
    )
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        await _feed_turn(pilot, scr, records[:second], whole_calls=True)
        assert any("Отчёт 0 попытка 1." in t for t in _texts(scr))
        scr._answer_flow = AnswerFlow()
        await _feed_turn(pilot, scr, records[second:], whole_calls=True)
        texts = _texts(scr)
        assert not any("Отчёт 0 попытка 1." in t for t in texts)
        assert sum("Отчёт 0 попытка 2." in t for t in texts) == 1


@pytest.mark.asyncio
async def test_copy_numbering_matches_feed_with_refused_attempt(tmp_path):
    """/copy N — те же ответы и в том же порядке, что видит человек в
    ленте: проза без повтора, отклонённая попытка не считается, принятый
    отчёт — последним (одно правило дедупа и свёртки)."""
    from gc_tui.services.export import agent_answers

    records = [*_refused_finish_turn(0, 2), *_giant_turn(1, 3)]
    answers = agent_answers([{"messages": records, "compacted_by": None}])
    assert answers[-1] == "Ход 1: смотрю шаг 3."
    assert "Отчёт 0 попытка 1." not in answers
    assert answers.count("Отчёт 0 попытка 2.") == 1
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        write_history(str(tmp_path / "t"), fixture)
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause(0.3)
        await _settled(pilot, scr)
        candidates = set(answers) | {"Отчёт 0 попытка 1."}
        shown = [
            text
            for text in (t.removeprefix("●\n") for t in _texts(scr))
            if text in candidates
        ]
    assert shown == answers


@pytest.mark.asyncio
async def test_trim_walks_feed_constant_times(tmp_path, monkeypatch):
    """Снятие десятка шагов за раз считает головы и границы шагов один
    раз на операцию, а не на каждый шаг и каждое обращение (R47 #2, #3):
    проходов по ленте за снятие — не больше нескольких, сколько бы шагов
    ни снималось."""
    from gc_tui.screens import trace_window

    _small_window(monkeypatch)
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        assert scr.attach_active_task(HISTORY_THREAD)
        await pilot.pause()
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 10**7)
        await _feed_turn(
            pilot, scr, _giant_turn(0, 120, outcome=False), whole_calls=True
        )
        assert scr._window_first == 0
        steps_before = len(scr._window_heads)
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 100)
        walks = []
        heads = trace_window.step_heads

        def counted(children, step_of):
            walks.append(1)
            return heads(children, step_of)

        monkeypatch.setattr(trace_window, "step_heads", counted)
        scr._window_trim("top")
        monkeypatch.setattr(trace_window, "step_heads", heads)
        dropped = steps_before - len(scr._window_heads)
        assert scr._window_first >= 8
        assert len(walks) <= 4, (len(walks), dropped)
