"""Второй край окна ленты (спека шага 2 окна ленты, §5.11): пока лента не
у низа, смонтированное держится в пределах «лимит + кусок» — встал кусок
с одного края, снимается противоположный, дальше двух запасов от
видимого; живой ход остаётся, между прочитанным и им — разрыв. Снизу
домонтируется перед разрывом, переход в конец при разрыве монтирует
хвост, как при открытии. Боевые числа политики, ход — как у T150
(десяток вызовов и длинный ответ с кодом) и один шаг: номера ходов в
тестах — номера шагов окна (`_open_session` это проверяет)."""

from __future__ import annotations

import asyncio
import gc
import json
import weakref

import pytest
from feed_dump import (
    HISTORY_THREAD,
    HistoryFixture,
    feed_dump,
    feed_live,
    write_history,
)
from gc_testkit.tui_support import pinned_snapshot_clock, wait_until
from gc_tui.services import trace_window_policy as policy
from test_feed_parity_baseline import _settled_dump
from test_trace_ephemera import _app, live_session
from textual import events

_SIZE = (100, 30)
TURNS = 40
CALLS = 9
LINES = 10
#: Подъём на столько ходов от конца — глубина замера §7; ход фикстуры
#: тяжёлый настолько, что глубина больше «лимит + кусок».
DEPTH = 20


# ---- ходы ------------------------------------------------------------------


def _message(payload: dict) -> dict:
    return {"type": "message", "payload": payload}


def _call(i: int, k: int) -> list[dict]:
    cid = f"call_{i}_{k}"
    command = f"ls каталог{k}"
    output = {
        "tool": "exec_command",
        "ok": True,
        "command": command,
        "exit_code": 0,
        "stdout": {"content": "a\nb"},
    }
    return [
        _message(
            {
                "arguments": json.dumps({"command": command}),
                "call_id": cid,
                "name": "exec_command",
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


def _turn(i: int, *, outcome: bool = True) -> list[dict]:
    """Ход: реплика, вызовы, ответ в много строк и итог."""
    user, answer = f"m_u{i:04d}", f"msg_a{i:04d}"
    text = "\n\n".join(
        f"ответ {i} абзац {k}" + ("\n\n```\nкод\n```" if k % 4 == 0 else "")
        for k in range(LINES)
    )
    records = [
        _message({"role": "user", "id": user, "content": f"запрос {i}"}),
    ]
    for k in range(CALLS):
        records += _call(i, k)
    records.append(
        _message(
            {
                "id": answer,
                "type": "message",
                "status": "completed",
                "role": "assistant",
                "content": [
                    {"type": "output_text", "annotations": [], "text": text}
                ],
            }
        )
    )
    if outcome:
        records.append(
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
            }
        )
    return records


def _records(n: int = TURNS) -> list[dict]:
    return [rec for i in range(n) for rec in _turn(i)]


async def _open_session(pilot, threads_dir):
    records = _records()
    fixture = HistoryFixture(
        records=tuple(records), segments=(tuple(records),), sidecar=()
    )
    write_history(str(threads_dir), fixture)
    scr = pilot.app.screen
    assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
    await pilot.pause(0.3)
    await _settled(pilot, scr)
    _single_step_turns(scr)
    return scr


async def _open_preview(pilot, *, outcomes: bool = False):
    """Превью истории; ``outcomes`` — с записями итога ходов."""
    from gc_tui.screens.preview import PreviewScreen

    messages = [
        {"payload": rec["payload"]} if rec["type"] == "message" else rec
        for rec in _records()
        if rec["type"] == "message" or outcomes
    ]
    screen = PreviewScreen(
        segments=[{"messages": messages, "index": 0, "compacted_by": None}],
        thread_id="abc12345",
        project_path="/p",
    )
    await pilot.app.push_screen(screen)
    await pilot.pause(0.3)
    await _settled(pilot, screen)
    _single_step_turns(screen)
    return screen


def _single_step_turns(scr) -> None:
    """Ход фикстуры — один шаг: номер хода — номер шага окна."""
    model = scr._window_model()
    assert model.step_starts == model.turn_starts


# ---- экран -----------------------------------------------------------------


def _trace(scr):
    return scr.query_one("#trace")


def _replies(scr) -> list[int]:
    from gc_tui.widgets.user_reply import UserReplyContainer

    texts = [
        w.plain_text.split()[-1]
        for w in _trace(scr).children
        if isinstance(w, UserReplyContainer)
    ]
    return [int(t) for t in texts if t.isdigit()]


async def _settled(pilot, scr) -> None:
    """Кусок не встаёт и не ждёт."""
    for _ in range(3):
        await wait_until(pilot, lambda: not scr._window_busy, "встал")
        await pilot.pause(0.1)


def _window_widgets(scr, *, pinned: int | None = None) -> int:
    """Виджетов поддерева ленты в ходах окна, без хода ``pinned``."""
    turns, counts = scr._window_mounted_counts()
    return sum(c for t, c in zip(turns, counts, strict=True) if t != pinned)


def _bound() -> int:
    return policy.WINDOW_LIMIT + policy.CHUNK_MIN


async def _wheel(pilot, scr, up: bool) -> None:
    trace = _trace(scr)
    event = events.MouseScrollUp if up else events.MouseScrollDown
    trace.post_message(event(trace, 5, 5, 0, 0, 0, False, False, False))
    await pilot.pause()
    if scr._window_busy:
        await _settled(pilot, scr)


async def _lift(pilot, scr, *, pinned: int | None) -> None:
    """Колесом вверх, пока не смонтирован ход ``DEPTH`` от конца; окно
    после каждого щелчка — в пределах «лимит + кусок» плюс закреплённое."""
    goal = TURNS - DEPTH
    for _ in range(3000):
        if scr._window_first <= goal:
            break
        await _wheel(pilot, scr, up=True)
        assert _window_widgets(scr, pinned=pinned) <= _bound()
    assert scr._window_first <= goal
    assert scr._stick_bottom is False


async def _jump_up(pilot, scr, *, pinned: int | None = TURNS - 1) -> None:
    """Как `_lift`, программной прокруткой к верху (быстрее колеса)."""
    goal = TURNS - DEPTH
    for _ in range(200):
        if scr._window_first <= goal:
            break
        _trace(scr).scroll_to(y=0, animate=False)
        await pilot.pause()
        await _settled(pilot, scr)
        assert _window_widgets(scr, pinned=pinned) <= _bound()
    assert scr._window_first <= goal
    assert scr._stick_bottom is False


def _consecutive(scr) -> None:
    """Ходы ленты — по порядку модели, без дублей; пропуск — только
    разрыв окна."""
    replies = _replies(scr)
    gap = scr._window_gap
    expected = [
        i
        for i in range(scr._window_first, scr._window_model().turn_count())
        if i not in gap
    ]
    assert replies == expected


# ---- (а) подъём держит окно ------------------------------------------------


@pytest.mark.asyncio
async def test_reading_up_bounded_with_gap_before_live_turn(tmp_path):
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        last = TURNS - 1
        await _lift(pilot, scr, pinned=last)
        # Низ прочитанного снят, последний ход на месте — разрыв.
        assert scr._window_gap
        assert scr._window_gap.stop == last
        assert _replies(scr)[-1] == last
        _consecutive(scr)


@pytest.mark.asyncio
async def test_continuous_climb_trims_between_chunks(tmp_path, monkeypatch):
    """Подъём подряд: следующий кусок начинается сразу, как встал
    прежний (прокрутка к верху пришла до раскладки), — снятие низа всё
    равно встаёт между кусками, окно не копится."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        trace = _trace(scr)
        settle = scr._window_settle

        def settle_then_next(*args) -> None:
            settle(*args)
            trace.scroll_to(y=0, animate=False, immediate=True)
            if not scr._window_busy and scr._window_first > 0:
                scr._window_prepend(focus=False)

        monkeypatch.setattr(scr, "_window_settle", settle_then_next)
        trace.scroll_to(y=0, animate=False)
        await pilot.pause()
        assert scr._stick_bottom is False
        worst = 0
        for _ in range(2000):
            if scr._window_first <= TURNS - DEPTH:
                break
            await pilot.pause()
            worst = max(worst, _window_widgets(scr, pinned=TURNS - 1))
        assert scr._window_first <= TURNS - DEPTH
        # Кусок в полёте — сверх «лимит + кусок» не больше хода-двух.
        assert worst <= _bound() + 200


@pytest.mark.asyncio
async def test_top_trim_while_reading_keeps_reading_row(tmp_path, monkeypatch):
    """Снятие сверху, пока пользователь не у низа, держит читаемую
    строку на месте (якорь, как у домонтирования сверху)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        trace = _trace(scr)
        # Сверху копится сверх лимита (снятие и предзагрузка стоят).
        trim = scr._window_trim
        monkeypatch.setattr(scr, "_window_trim", lambda *_: None)
        monkeypatch.setattr(scr, "_window_prefetch", lambda: None)
        while _window_widgets(scr, pinned=TURNS - 1) <= _bound() + 200:
            assert scr._window_prepend(focus=False)
            await _settled(pilot, scr)
        # Читаемая строка — в нижней части прочитанного.
        head = scr._window_gap_head()
        trace.scroll_to(
            y=head.virtual_region.y - 4 * trace.container_size.height,
            animate=False,
        )
        await pilot.pause()
        await pilot.pause()
        assert scr._stick_bottom is False
        row = trace.first_visible()
        at = row.region.y
        first = scr._window_first
        trim("top")
        await pilot.pause()
        await pilot.pause()
        assert scr._window_first > first
        assert row.is_attached
        assert row.region.y == at


# ---- (б) живой ход при чтении выше -----------------------------------------


@pytest.mark.asyncio
async def test_live_turn_while_reading_up_keeps_gap(tmp_path):
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        trace = _trace(scr)
        row = trace.first_visible()
        at = row.region.y
        live = _turn(TURNS, outcome=False)
        for n in range(1, len(live) + 1, 4):
            feed_live(scr, live[n - 1 : n + 3], splits=())
            await pilot.pause()
            await _settled(pilot, scr)
            assert _window_widgets(scr, pinned=TURNS) <= _bound()
        assert scr._stick_bottom is False
        # Живой ход смонтирован и дописан в конец ленты, разрыв на месте.
        assert TURNS in scr._window_heads
        assert scr._window_gap
        assert scr._window_gap.stop <= TURNS
        assert _replies(scr)[-1] == TURNS
        assert row.region.y == at
        _consecutive(scr)


# ---- (в) прокрутка вниз закрывает разрыв -----------------------------------


@pytest.mark.asyncio
async def test_scroll_down_closes_gap_in_order(tmp_path):
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _lift(pilot, scr, pinned=TURNS - 1)
        assert scr._window_gap
        for _ in range(5000):
            if not scr._window_gap or scr._stick_bottom:
                break
            await _wheel(pilot, scr, up=False)
            assert _window_widgets(scr, pinned=TURNS - 1) <= _bound()
            _consecutive(scr)
        # Разрыв закрыт домонтированием снизу, а не переходом в конец.
        assert not scr._window_gap
        assert scr._stick_bottom is False
        _consecutive(scr)


# ---- (г) переход в конец при разрыве ---------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["to_end", "submit"])
async def test_to_end_with_gap_mounts_tail_as_on_open(tmp_path, how):
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        tail = policy.tail_steps(
            scr._window_estimates(scr._window_model()), policy.TAIL_MIN
        )
        await _jump_up(pilot, scr)
        gap = scr._window_gap
        assert gap
        inserted: list[range] = []
        insert = scr._window_insert

        def spy_insert(steps, *, before):
            inserted.append(steps)
            return insert(steps, before=before)

        scr._window_insert = spy_insert
        if how == "to_end":
            scr._scroll_to_bottom()
        else:
            scr._mount_user_reply(
                "новый запрос", state="pending", client_id="c1"
            )
        await pilot.pause()
        await _settled(pilot, scr)
        trace = _trace(scr)
        # Как при открытии: хвост по оценке и догрузка, пока ленте не
        # хватает запаса (при открытии высота блоков кода до раскладки ещё
        # не известна — догрузка там может взять лишний ход). Длинный
        # разрыв не домонтирован целиком: прочитанное снято, перед
        # закреплённым низом встал хвост.
        assert not scr._window_gap
        assert inserted and inserted[0].start > gap.start
        assert scr._window_first <= tail.start
        assert not policy.underfilled(
            trace.max_scroll_y, trace.container_size.height
        )
        assert _replies(scr) == list(range(scr._window_first, TURNS))
        assert scr._stick_bottom is True
        assert trace.scroll_y == trace.max_scroll_y


# ---- (д) снятый снизу ход возвращается с эфемерным -------------------------


@pytest.mark.asyncio
async def test_bottom_trimmed_turn_returns_with_ephemera(
    tmp_path, monkeypatch
):
    with pinned_snapshot_clock(monkeypatch):
        async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
            scr = await live_session(pilot, prelude=_turn(0))
            from test_trace_ephemera import _expand_views

            _expand_views(scr)
            before = await _settled_dump(pilot)
            assert scr._window_first == 0
            # Ход с мыслью, «!» и раскрытыми блоками — снизу окна.
            assert scr.forget_step(1)
            await pilot.pause()
            assert scr._window_gap == range(1, 2)
            assert not any(
                "echo hi" in t for _c, t, _ in feed_dump(_trace(scr))
            )
            assert scr._window_append(focus=False)
            await _settled(pilot, scr)
            assert not scr._window_gap
            assert await _settled_dump(pilot) == before


# ---- (е) превью — то же правило --------------------------------------------


@pytest.mark.asyncio
async def test_preview_reading_up_bounded_then_to_end(tmp_path):
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _open_preview(pilot)
        opened = screen._window_first
        await _jump_up(pilot, screen, pinned=None)
        # Живого хода нет: разрыв доходит до конца истории.
        assert screen._window_gap
        assert screen._window_gap.stop == TURNS
        _consecutive(screen)
        screen.action_to_end()
        await pilot.pause()
        await _settled(pilot, screen)
        trace = _trace(screen)
        # Хвост, как при открытии: до запаса ленты, без прочитанного.
        assert not screen._window_gap
        assert opened <= screen._window_first
        assert screen._window_first > TURNS - DEPTH
        assert not policy.underfilled(
            trace.max_scroll_y, trace.container_size.height
        )
        assert _replies(screen) == list(range(screen._window_first, TURNS))
        assert screen._stick_bottom is True


@pytest.mark.asyncio
async def test_preview_bottom_trimmed_last_turn_returns_settled(tmp_path):
    """В превью итога «сейчас» нет: последний ход, снятый снизу при
    подъёме, возвращается со строкой итога, как у всех, — не свежей."""
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp
    from gc_tui.widgets.turn_line import TurnLine

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _open_preview(pilot, outcomes=True)
        await _jump_up(pilot, screen, pinned=None)
        assert TURNS - 1 in screen._window_gap
        screen.action_to_end()
        await pilot.pause()
        await _settled(pilot, screen)
        lines = list(_trace(screen).query(TurnLine))
        assert TURNS - 1 not in screen._window_gap
        assert lines
        assert [line for line in lines if line.is_fresh] == []


# ---- пульсация, видимое, закреплённое --------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("edge", ["top", "bottom"])
async def test_wheel_reversals_do_not_pulse(tmp_path, monkeypatch, edge):
    """Колесо туда-обратно на глубине: снятый ход не домонтируется снова
    — снятие лежит дальше двух запасов, кусок встаёт на одном. ``bottom``
    — у порога домонтирования перед разрывом."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _lift(pilot, scr, pinned=TURNS - 1)
        trace = _trace(scr)
        if edge == "bottom":
            # До разрыва — на строку дальше запаса: первый же щелчок вниз
            # домонтирует кусок перед ним.
            height = trace.container_size.height
            reach = (policy.PREFETCH_SCREENS + 1) * height + 1
            trace.scroll_to(y=scr._window_gap_y() - reach, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        forgotten: list[int] = []
        forget = scr.forget_step

        def counted(turn: int) -> bool:
            done = forget(turn)
            if done:
                forgotten.append(turn)
            return done

        monkeypatch.setattr(scr, "forget_step", counted)
        mounted: list[int] = []
        render = scr._window_render

        def rendered(turns, *, before):
            mounted.extend(turns)
            return render(turns, before=before)

        monkeypatch.setattr(scr, "_window_render", rendered)
        for _ in range(10):
            for up in (False,) * 3 + (True,) * 3:
                await _wheel(pilot, scr, up=up)
        await _settled(pilot, scr)
        assert not set(forgotten) & set(mounted)
        assert len(mounted) <= 1
        if edge == "bottom":
            # Край разрыва достигнут: кусок встал, разрыв ещё открыт.
            assert mounted
            assert scr._window_gap


def _turns_on_screen(scr) -> set[int]:
    """Ходы, чьи строки на экране, — по карте экрана, не по раскладке
    окна."""
    view = _trace(scr).content_region
    return {
        turn
        for turn in scr._window_heads
        if any(view.overlaps(w.region) for w in scr._window_step_widgets(turn))
    }


@pytest.mark.asyncio
async def test_turns_on_screen_never_trimmed(tmp_path, monkeypatch):
    """Лимит меньше видимого, удержание — только видимое: снятие с обоих
    краёв снимает всё, кроме ходов на экране (ход — минимальная
    единица)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 10)
        monkeypatch.setattr(policy, "CHUNK_MIN", 1)
        monkeypatch.setattr(policy, "PREFETCH_SCREENS", 0)
        # Верхний ход окна — над экраном: снимать сверху есть что.
        trace = _trace(scr)
        second = scr._window_heads[scr._window_first + 1]
        trace.scroll_to(y=trace.placed_top(second), animate=False)
        await pilot.pause()
        await _settled(pilot, scr)
        on_screen = _turns_on_screen(scr)
        first, gap = scr._window_first, scr._window_gap
        assert on_screen
        scr._window_trim("bottom")
        scr._window_trim("top")
        # Сняты ходы с обоих краёв, ходы на экране — на месте.
        assert scr._window_first > first
        assert scr._window_gap.start < gap.start
        assert on_screen <= set(scr._window_heads)
        assert _trace(scr).first_visible().is_attached


@pytest.mark.asyncio
@pytest.mark.parametrize("how", ["focus", "hook"])
async def test_pinned_turn_in_read_range_stops_trim(
    tmp_path, monkeypatch, how
):
    """Закреплённый ход в середине прочитанного: снятие снизу снимает ходы
    под ним и на нём останавливается, второго разрыва нет. ``hook`` —
    закрепление, которое знает только окно (`_window_pinned`): «забыть
    ход» сам его не откажет."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        monkeypatch.setattr(policy, "PREFETCH_SCREENS", 0)
        gap = scr._window_gap
        pinned = gap.start - 2
        assert pinned > scr._window_first
        assert pinned not in scr._window_kept_steps()
        if how == "focus":
            row = scr._window_step_widgets(pinned)[0]
            row.can_focus = True
            scr.set_focus(row, scroll_visible=False)
            await pilot.pause()
        else:
            pinned_turn = scr._window_pinned
            monkeypatch.setattr(
                scr,
                "_window_pinned",
                lambda turn: turn == pinned or pinned_turn(turn),
            )
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 10)
        monkeypatch.setattr(policy, "CHUNK_MIN", 1)
        scr._window_trim("bottom")
        await pilot.pause()
        assert gap.start - 1 not in scr._window_heads
        assert pinned in scr._window_heads
        assert scr._window_gap.start == pinned + 1
        _consecutive(scr)


# ---- низ при открытом разрыве ----------------------------------------------


@pytest.mark.asyncio
async def test_touching_bottom_with_gap_does_not_stick(tmp_path):
    """Превью: разрыв в конце, физический низ — не конец истории. Касание
    низа липкость не включает, снизу домонтируется."""
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _open_preview(pilot)
        await _jump_up(pilot, screen, pinned=None)
        gap = screen._window_gap
        trace = _trace(screen)
        trace.scroll_to(y=trace.max_scroll_y, animate=False)
        await pilot.pause()
        await _settled(pilot, screen)
        assert screen._stick_bottom is False
        assert screen._window_gap.start > gap.start
        _consecutive(screen)


@pytest.mark.asyncio
async def test_fast_scroll_into_tail_closes_gap_without_jump(tmp_path):
    """Прокрутка за разрыв в закреплённый низ: разрыв закрывается, строка
    чтения стоит на месте."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        trace = _trace(scr)
        head = scr._window_gap_head()
        trace.scroll_to(y=head.virtual_region.y + 2, animate=False)
        await pilot.pause()
        row = head
        at = row.region.y
        await _settled(pilot, scr)
        await pilot.pause()
        assert not scr._window_gap
        assert scr._stick_bottom is False
        assert row.region.y == at
        _consecutive(scr)


@pytest.mark.asyncio
async def test_event_down_at_gap_mounts_and_focuses(tmp_path):
    """↓ в режиме событий на последнем событии перед разрывом — зеркало
    ↑: кусок перед разрывом и фокус на его первое событие."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        gap = scr._window_gap
        trace = _trace(scr)
        head = scr._window_gap_head()
        children = list(trace.children)
        before_gap = [
            w
            for w in children[: children.index(head)]
            if w.can_focus and w.display
        ]
        before_gap[-1].focus()
        await pilot.pause()
        assert scr._event_nav_key("down")
        await _settled(pilot, scr)
        assert scr._window_gap.start > gap.start
        first_new = scr._window_heads[gap.start]
        focusable = [w for w in trace.children if w.can_focus and w.display]
        assert scr.focused is next(
            w
            for w in focusable[focusable.index(before_gap[-1]) + 1 :]
            if trace.children.index(w) >= trace.children.index(first_new)
        )
        _consecutive(scr)


@pytest.mark.asyncio
@pytest.mark.parametrize("where", ["inside", "last"])
async def test_event_down_in_pinned_bottom_stays_there(tmp_path, where):
    """↓ в закреплённом низу при открытом разрыве — не край разрыва:
    фокус на следующее событие низа (на последнем — на месте), кусок не
    домонтируется, фокус вверх не уходит."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        gap = scr._window_gap
        children = list(_trace(scr).children)
        tail = [
            w
            for w in children[children.index(scr._window_gap_head()) :]
            if w.can_focus and w.display
        ]
        assert len(tail) >= 2
        current = tail[0] if where == "inside" else tail[-1]
        scr.set_focus(current, scroll_visible=False)
        await pilot.pause()
        assert scr._event_nav_key("down")
        assert not scr._window_busy
        assert scr._window_gap == gap
        await pilot.pause()
        assert scr.focused is (tail[1] if where == "inside" else current)


@pytest.mark.asyncio
async def test_island_does_not_block_sticking(tmp_path, monkeypatch):
    """Остров: в прочитанном остался только закреплённый ход (открытый
    «!»), разрыв под ним не закрыть. Он не мешает «у низа»: колесо к низу
    возвращает липкость, прокрутка в хвосте разрыв заново не
    закрывает."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        island = scr._window_first + 1
        bang = scr._window_heads[island]
        monkeypatch.setattr(scr._ephemera, "bang_open_widgets", lambda: [bang])
        trace = _trace(scr)
        for _ in range(5):
            trace.scroll_to(y=trace.max_scroll_y, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        assert scr._window_first == island
        assert scr._window_gap.start == island + 1
        assert scr._stick_bottom is True
        closes: list[int] = []
        close = scr._window_close_gap

        def counted() -> None:
            closes.append(1)
            close()

        monkeypatch.setattr(scr, "_window_close_gap", counted)
        for _ in range(10):
            if not scr._stick_bottom:
                break
            await _wheel(pilot, scr, up=True)
        await _wheel(pilot, scr, up=True)
        assert scr._stick_bottom is False
        assert scr._window_gap_y() <= trace.scroll_y
        assert closes == []
        for _ in range(10):
            if scr._stick_bottom:
                break
            await _wheel(pilot, scr, up=False)
        assert scr._stick_bottom is True
        assert scr._window_gap


async def _short_gap_reading_top(pilot, scr, monkeypatch, *, pinned):
    """Разрыв в один ход и короткий, окно сверх «лимит + кусок», читаемая
    строка — у верха прочитанного (пока копится, снятие и предзагрузка
    стоят)."""
    await _jump_up(pilot, scr, pinned=pinned)
    monkeypatch.setattr(scr, "_window_trim", lambda *_: None)
    monkeypatch.setattr(scr, "_window_prefetch", lambda: None)
    estimates = scr._window_estimates(scr._window_model())

    def gap_widgets() -> int:
        gap = scr._window_gap
        return sum(estimates[gap.start : gap.stop])

    while len(scr._window_gap) > 1:
        assert scr._window_append(focus=False)
        await _settled(pilot, scr)
    while _window_widgets(scr, pinned=pinned) <= _bound() + 200:
        assert scr._window_prepend(focus=False)
        await _settled(pilot, scr)
    assert scr._window_gap
    _trace(scr).scroll_to(y=0, animate=False)
    await pilot.pause()
    monkeypatch.undo()
    # Ход истории с итогом чуть больше хвоста по оценке — разрыв в один ход
    # короткий.
    monkeypatch.setattr(
        policy, "TAIL_MIN", max(policy.TAIL_MIN, gap_widgets())
    )
    assert policy.short_gap(gap_widgets())


@pytest.mark.asyncio
async def test_to_end_short_gap_trims_by_view_at_end(tmp_path, monkeypatch):
    """Короткий разрыв, переход в конец: снятие сверху меряет по виду у
    низа, а не по прочитанному месту — окно в пределах «лимит + кусок»."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        last = TURNS - 1
        await _short_gap_reading_top(pilot, scr, monkeypatch, pinned=last)
        scr._scroll_to_bottom()
        await pilot.pause()
        await _settled(pilot, scr)
        assert not scr._window_gap
        assert scr._stick_bottom is True
        assert _window_widgets(scr, pinned=last) <= _bound()
        _consecutive(scr)


@pytest.mark.asyncio
async def test_preview_to_end_short_gap_trims_by_view_at_end(
    tmp_path, monkeypatch
):
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _open_preview(pilot)
        await _short_gap_reading_top(pilot, screen, monkeypatch, pinned=None)
        screen.action_to_end()
        await pilot.pause()
        await _settled(pilot, screen)
        assert not screen._window_gap
        assert screen._stick_bottom is True
        assert _window_widgets(screen) <= _bound()
        _consecutive(screen)


@pytest.mark.asyncio
async def test_stuck_trim_without_layout_keeps_last_turn(
    tmp_path, monkeypatch
):
    """Раскладки нет (иная версия Textual): у низа удерживаемых нет, но
    последний ход не снимается — даже больше лимита (превью: живого хода
    нет, закреплённого нет)."""
    from gc_testkit.tui_support import tui_settings
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test(size=_SIZE) as pilot:
        screen = await _open_preview(pilot)
        assert screen._stick_bottom is True
        monkeypatch.setattr(_trace(screen), "placed_between", lambda *_: None)
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 0)
        monkeypatch.setattr(policy, "CHUNK_MIN", 0)
        screen._window_trim("top")
        assert set(screen._window_heads) == {TURNS - 1}
        assert screen._window_first == TURNS - 1


# ---- (е) память при непрерывном подъёме -------------------------------------


@pytest.mark.asyncio
async def test_continuous_climb_frees_trimmed_turns(tmp_path, monkeypatch):
    """Подъём кусок за куском без простоя (следующий — сразу, как встал
    прежний): снятые ходы освобождаются по ходу подъёма, а не копятся до
    простоя ленты."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        trace = _trace(scr)
        settle, forget = scr._window_settle, scr._window_forget_step
        trimmed: weakref.WeakSet = weakref.WeakSet()

        def settle_then_next(*args) -> None:
            settle(*args)
            trace.scroll_to(y=0, animate=False, immediate=True)
            if not scr._window_busy and scr._window_first > 0:
                scr._window_prepend(focus=False)

        def forget_noted(turn: int) -> bool:
            for widget in scr._window_step_widgets(turn):
                trimmed.update(widget.walk_children(with_self=True))
            return forget(turn)

        monkeypatch.setattr(scr, "_window_settle", settle_then_next)
        monkeypatch.setattr(scr, "_window_forget_step", forget_noted)
        trace.scroll_to(y=0, animate=False)
        for _ in range(3000):
            await asyncio.sleep(0.02)
            if scr._window_first == 0 and not scr._window_busy:
                break
        assert scr._window_first == 0
        gc.collect()
        # Снятое держат только ссылки окна на последний кусок открытия —
        # не всё снятое за подъём.
        alive = [w for w in trimmed if not w.is_attached]
        assert len(alive) <= policy.WINDOW_LIMIT // 4


# ---- возврат к низу прокруткой при закреплённых в прочитанном --------------


def _pin_turns(scr, monkeypatch, how: str) -> list[int]:
    """Закрепить старые ходы прочитанного диапазона: открытый «!» (как в
    `test_trace_trim`) или фокус на старой строке. ``two`` — «!» в двух
    ходах с обычными между ними; ``one`` — в одном; ``focus`` — фокус на
    старой строке; ``focus_and_bang`` — фокус в одном ходе, «!» в
    другом. Возвращает ходы с «!»: фокус переход к низу уводит домой
    (§5.11), его ход закреплённым не остаётся."""
    low = scr._window_first + 1
    high = scr._window_gap.start - 2
    assert high - low >= 2
    turns = {
        "two": [low, high],
        "one": [high],
        "focus": [low],
        "focus_and_bang": [low, high],
    }[how]
    banged = turns[1:] if how.startswith("focus") else turns
    heads = [scr._window_heads[t] for t in banged]
    monkeypatch.setattr(scr._ephemera, "bang_open_widgets", lambda: heads)
    if how.startswith("focus"):
        row = scr._window_step_widgets(low)[0]
        row.can_focus = True
        scr.set_focus(row, scroll_visible=False)
    for turn in turns:
        assert scr._step_pinned(turn)
    return banged


async def _down_to_bottom(pilot, scr, how: str) -> None:
    """Вниз к физическому низу: колесом или прыжком (ползунок)."""
    trace = _trace(scr)
    at_bottom = 0
    for _ in range(3000):
        if scr._stick_bottom or at_bottom > 5:
            return
        bottom = trace.scroll_y >= trace.max_scroll_y
        at_bottom = at_bottom + 1 if bottom else 0
        if how == "jump":
            trace.scroll_to(y=trace.max_scroll_y, animate=False)
            await pilot.pause()
            await _settled(pilot, scr)
        else:
            await _wheel(pilot, scr, up=False)


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("how", "move"),
    [
        ("two", "wheel"),
        ("two", "jump"),
        ("one", "jump"),
        ("focus", "jump"),
        ("focus_and_bang", "jump"),
    ],
)
async def test_scroll_back_to_bottom_sticks_with_pinned_in_read_range(
    tmp_path, monkeypatch, how, move
):
    """Идёт живой ход, пользователь ушёл вверх (разрыв), закрепил старые
    ходы и вернулся к физическому низу прокруткой — колесом или прыжком
    (ползунок): лента прилипла, закреплённые на месте, новый вывод живого
    хода — у низа. Разрыв закрыт или остался под прочитанным, которое не
    ужать (оба конца закреплены), — низу он не мешает."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        live = _turn(TURNS, outcome=False)
        feed_live(scr, live[:4], splits=())
        await pilot.pause()
        await _settled(pilot, scr)
        await _lift(pilot, scr, pinned=TURNS)
        assert scr._window_gap
        pinned = _pin_turns(scr, monkeypatch, how)
        await _down_to_bottom(pilot, scr, move)
        trace = _trace(scr)
        assert scr._stick_bottom is True
        assert set(pinned) <= set(scr._window_heads)
        for n in range(4, len(live), 4):
            feed_live(scr, live[n : n + 4], splits=())
            await pilot.pause()
            await _settled(pilot, scr)
        await pilot.pause()
        assert scr._stick_bottom is True
        assert trace.scroll_y == trace.max_scroll_y
        assert set(pinned) <= set(scr._window_heads)
        _consecutive(scr)
