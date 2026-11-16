"""Второй край окна ленты (спека шага 2 окна ленты, §5.11): пока лента не
у низа, смонтированное держится в пределах «лимит + кусок» — встал кусок
с одного края, снимается противоположный, дальше двух запасов от
видимого; живой ход остаётся, между прочитанным и им — разрыв. Снизу
домонтируется перед разрывом, переход в конец при разрыве монтирует
хвост, как при открытии. Боевые числа политики, ход — как у T150
(десяток вызовов и длинный ответ)."""

from __future__ import annotations

import json

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
CALLS = 10
LINES = 10
#: Подъём на столько ходов от конца — глубина замера §7.
DEPTH = 21


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
    text = "\n".join(f"ответ {i} строка {k}" for k in range(LINES))
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
    return scr


async def _open_preview(pilot):
    from gc_tui.screens.preview import PreviewScreen

    messages = [
        {"payload": rec["payload"]}
        for rec in _records()
        if rec["type"] == "message"
    ]
    screen = PreviewScreen(
        segments=[{"messages": messages, "index": 0, "compacted_by": None}],
        thread_id="abc12345",
        project_path="/p",
    )
    await pilot.app.push_screen(screen)
    await pilot.pause(0.3)
    await _settled(pilot, screen)
    return screen


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
        opened = scr._window_first
        await _jump_up(pilot, scr)
        assert scr._window_gap
        if how == "to_end":
            scr._scroll_to_bottom()
        else:
            scr._mount_user_reply(
                "новый запрос", state="pending", client_id="c1"
            )
        await pilot.pause()
        await _settled(pilot, scr)
        trace = _trace(scr)
        assert not scr._window_gap
        assert scr._window_first == opened
        assert _replies(scr) == list(range(opened, TURNS))
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
            assert scr.forget_turn(1)
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


# ---- пульсация, видимое, закреплённое --------------------------------------


@pytest.mark.asyncio
async def test_wheel_reversals_do_not_pulse(tmp_path, monkeypatch):
    """Колесо туда-обратно на глубине: снятый ход не домонтируется снова
    — снятие лежит дальше двух запасов, кусок встаёт на одном."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _lift(pilot, scr, pinned=TURNS - 1)
        forgotten: list[int] = []
        forget = scr.forget_turn

        def counted(turn: int) -> bool:
            done = forget(turn)
            if done:
                forgotten.append(turn)
            return done

        monkeypatch.setattr(scr, "forget_turn", counted)
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


@pytest.mark.asyncio
async def test_turns_on_screen_never_trimmed(tmp_path, monkeypatch):
    """Лимит меньше видимого: снятие не трогает ходы на экране и рядом —
    окно остаётся выше лимита (ход — минимальная единица)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        monkeypatch.setattr(policy, "WINDOW_LIMIT", 10)
        monkeypatch.setattr(policy, "CHUNK_MIN", 1)
        trace = _trace(scr)
        visible = scr._window_kept_turns()
        assert visible
        scr._window_trim("bottom")
        scr._window_trim("top")
        await pilot.pause()
        assert visible <= set(scr._window_heads)
        assert trace.first_visible().is_attached


@pytest.mark.asyncio
async def test_pinned_turn_in_read_range_stops_trim(tmp_path):
    """Ход в фокусе ниже читаемого: снятие снизу на нём останавливается,
    второго разрыва нет."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await _open_session(pilot, tmp_path / "t")
        await _jump_up(pilot, scr)
        gap = scr._window_gap
        pinned = gap.start - 1
        row = scr._window_turn_widgets(pinned)[0]
        row.can_focus = True
        scr.set_focus(row, scroll_visible=False)
        await pilot.pause()
        for _ in range(4):
            if not scr._window_prepend(focus=False):
                break
            await _settled(pilot, scr)
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
