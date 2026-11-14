"""Окно ленты (спека шага 2 окна, §3.1, §3.2, §5.3): смонтирован хвост,
история домонтируется сверху после прокрутки пользователем, по
возвращении вниз окно ужимается до лимита. Превью — на окне."""

from gc_testkit.tui_support import tui_settings, wait_until
import pytest
from textual.screen import Screen

from gc_tui import textual_compat
from gc_tui.screens.trace_follow import TraceFollowMixin, trace_overlays
from gc_tui.screens.trace_window import TraceWindowMixin
from gc_tui.services import trace_window_policy as policy
from gc_tui.services.history_model import HistoryInputs, build_history

TURNS = 30
# Боевые числа политики — до подмены фикстурой.
_POLICY = {
    name: getattr(policy, name)
    for name in ("TAIL_MIN", "CHUNK_MIN", "WINDOW_LIMIT", "ITEM_WIDGETS")
}


@pytest.fixture(autouse=True)
def _small_window(monkeypatch):
    """Окно в несколько ходов: ход фикстуры — реплика и ответ."""
    monkeypatch.setattr(policy, "TAIL_MIN", 30)
    monkeypatch.setattr(policy, "CHUNK_MIN", 24)
    monkeypatch.setattr(policy, "WINDOW_LIMIT", 80)


def _msg(mid, role, content):
    return {"payload": {"id": mid, "role": role, "content": content}}


def _segments(
    tag: str = "запрос", turns: int = TURNS, lines: int = 10
) -> list[dict]:
    msgs = []
    for i in range(turns):
        msgs.append(_msg(f"u{i}", "user", f"{tag} {i}"))
        answer = "\n".join(f"ответ {i} строка {k}" for k in range(lines))
        msgs.append(_msg(f"a{i}", "assistant", answer))
    return [{"messages": msgs, "index": 0, "compacted_by": None}]


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _preview(pilot):
    from gc_tui.screens.preview import PreviewScreen

    screen = PreviewScreen(
        segments=_segments(), thread_id="abc12345", project_path="/p"
    )
    await pilot.app.push_screen(screen)
    await pilot.pause(0.3)
    return screen


def _trace(screen):
    return screen.query_one("#trace")


def _replies(screen) -> list[str]:
    from gc_tui.widgets.user_reply import UserReplyContainer

    return [w.plain_text for w in _trace(screen).query(UserReplyContainer)]


def _subtree(screen) -> int:
    return len(_trace(screen).walk_children())


async def _user_scroll(pilot, screen, y: float) -> None:
    _trace(screen).scroll_to(y=y, animate=False)
    await pilot.pause()
    await pilot.pause()


async def _read_to_start(pilot, screen) -> None:
    for _ in range(40):
        if screen._window_first == 0:
            break
        await _user_scroll(pilot, screen, 0)
        await pilot.pause(0.1)
    assert screen._window_first == 0


async def _filled(pilot, screen) -> None:
    """Догрузка открытия закончилась: кусок не встаёт и не ждёт."""
    for _ in range(3):
        await wait_until(pilot, lambda: not screen._window_busy, "встал")
        await pilot.pause(0.2)


def _focusable(screen) -> list:
    return [w for w in _trace(screen).children if w.can_focus and w.display]


# ---- превью на окне --------------------------------------------------------


@pytest.mark.asyncio
async def test_preview_mounts_tail_then_history_on_scroll(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        tail = _replies(screen)
        assert 0 < len(tail) < TURNS
        assert tail[-1] == f"запрос {TURNS - 1}"
        first = screen._window_first
        await _user_scroll(pilot, screen, 0)
        await wait_until(
            pilot, lambda: screen._window_first < first, "кусок сверху"
        )
        await _read_to_start(pilot, screen)
        assert _replies(screen) == [f"запрос {i}" for i in range(TURNS)]


@pytest.mark.asyncio
async def test_wheel_up_after_open_fill_reaches_history(tmp_path, monkeypatch):
    """Короткий хвост (боевые числа политики): догрузка открытия растит
    ленту у низа. Колесо вверх снимает прилипание к низу и домонтирует
    кусок сверху — история выше хвоста достижима."""
    from textual import events

    for name, value in _POLICY.items():
        monkeypatch.setattr(policy, name, value)
    # Хвост ниже одного экрана: лента прижата к низу на нуле.
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        from gc_tui.screens.preview import PreviewScreen

        screen = PreviewScreen(
            segments=_segments(turns=200, lines=1),
            thread_id="abc12345",
            project_path="/p",
        )
        await pilot.app.push_screen(screen)
        await _filled(pilot, screen)
        trace = _trace(screen)
        tail = policy.tail_turns(
            screen._window_estimates(screen._model), policy.TAIL_MIN
        )
        assert screen._window_first < tail.start
        first = screen._window_first
        for _ in range(40):
            trace.post_message(
                events.MouseScrollUp(trace, 5, 5, 0, 0, 0, False, False, False)
            )
            await pilot.pause(0.02)
        await wait_until(
            pilot, lambda: screen._window_first < first, "кусок сверху"
        )
        assert screen._stick_bottom is False


@pytest.mark.asyncio
async def test_prefetch_only_after_user_scroll(tmp_path, monkeypatch):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        await _filled(pilot, screen)
        first = screen._window_first
        assert screen._stick_bottom is True
        monkeypatch.setattr(policy, "near_top", lambda *_: True)
        screen._window_prefetch()
        await pilot.pause(0.3)
        assert screen._window_first == first


@pytest.mark.asyncio
async def test_underfilled_tail_loads_history_without_input(tmp_path):
    """Хвост по оценке ниже запаса ленты — история догружается сама, у
    низа, пока ленте не хватит запаса."""
    async with _app(tmp_path).run_test(size=(100, 60)) as pilot:
        screen = await _preview(pilot)
        tail = policy.tail_turns(
            screen._window_estimates(screen._model), policy.TAIL_MIN
        )
        await _filled(pilot, screen)
        trace = _trace(screen)
        assert 0 < screen._window_first < tail.start
        assert not policy.underfilled(
            trace.max_scroll_y, trace.container_size.height
        )
        assert screen._stick_bottom is True
        assert trace.scroll_y == trace.max_scroll_y


@pytest.mark.asyncio
async def test_underfilled_short_history_loads_to_start(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 200)) as pilot:
        screen = await _preview(pilot)
        await _filled(pilot, screen)
        assert screen._window_first == 0
        assert _replies(screen) == [f"запрос {i}" for i in range(TURNS)]


@pytest.mark.asyncio
async def test_shrink_after_return_while_chunk_mounting(tmp_path, monkeypatch):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        await _filled(pilot, screen)
        monkeypatch.setattr(screen, "_window_prefetch", lambda: None)
        await _user_scroll(pilot, screen, 0)
        assert screen._stick_bottom is False
        limit = policy.WINDOW_LIMIT + policy.CHUNK_MIN
        while _subtree(screen) <= limit:
            assert screen._window_prepend(focus=False)
            await wait_until(pilot, lambda: not screen._window_busy, "встал")
        assert screen._window_first > 0

        def prepend_then_bottom() -> None:
            screen._window_prepend(focus=False)
            screen._scroll_to_bottom()

        pilot.app.call_later(prepend_then_bottom)
        await wait_until(
            pilot,
            lambda: _subtree(screen) <= policy.WINDOW_LIMIT,
            "окно ужато после куска",
        )
        assert screen._stick_bottom is True


@pytest.mark.asyncio
async def test_no_prefetch_while_selecting(tmp_path, monkeypatch):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        first = screen._window_first
        monkeypatch.setattr(screen, "_ui_frozen_for_select", lambda: True)
        screen._stick_bottom = False
        await _user_scroll(pilot, screen, 0)
        screen._window_prefetch()
        await pilot.pause(0.3)
        assert screen._window_first == first


@pytest.mark.asyncio
async def test_preview_shrinks_on_return_to_bottom(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        await _read_to_start(pilot, screen)
        assert _subtree(screen) > policy.WINDOW_LIMIT + policy.CHUNK_MIN
        await pilot.press("end")
        await wait_until(
            pilot,
            lambda: _subtree(screen) <= policy.WINDOW_LIMIT,
            "окно ужато до лимита",
        )
        replies = _replies(screen)
        assert "запрос 0" not in replies
        assert replies[-1] == f"запрос {TURNS - 1}"
        assert screen._window_first > 0
        assert screen._stick_bottom is True


@pytest.mark.asyncio
async def test_preview_shrink_spares_focused_turn(tmp_path):
    """Фокус на строке старого хода: возврат к низу его не снимает —
    ужатие останавливается на нём, фокус на месте."""
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        await _read_to_start(pilot, screen)
        await _filled(pilot, screen)
        top = _focusable(screen)[0]
        top.focus()
        await pilot.pause()
        screen._scroll_to_bottom()
        await pilot.pause(0.3)
        await _filled(pilot, screen)
        assert screen._window_first == 0
        assert top.is_attached
        assert screen.focused is top


@pytest.mark.asyncio
async def test_prepend_keeps_first_visible_row(tmp_path, monkeypatch):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        trace = _trace(screen)
        prefetch = screen._window_prefetch
        monkeypatch.setattr(screen, "_window_prefetch", lambda: None)
        await _user_scroll(pilot, screen, 1)
        held = trace.first_visible()
        row = held.region.y
        first = screen._window_first
        prefetch()
        await wait_until(
            pilot, lambda: not textual_compat._PAINT_HOLDS, "отпущено"
        )
        await pilot.pause()
        assert screen._window_first < first
        assert held.region.y == row


@pytest.mark.asyncio
async def test_edge_up_mounts_and_focuses(tmp_path, monkeypatch):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        # Фокус на верх окна прокручивает к нему — домонтирование по
        # прокрутке здесь не проверяется.
        monkeypatch.setattr(screen, "_window_prefetch", lambda: None)
        top = _focusable(screen)[0]
        top.focus()
        await pilot.pause()
        first = screen._window_first
        assert screen._window_edge_up() is True
        await wait_until(
            pilot,
            lambda: screen.focused is not top and screen.focused is not None,
            "фокус на куске сверху",
        )
        events = _focusable(screen)
        assert screen._window_first < first
        assert events.index(screen.focused) == events.index(top) - 1


@pytest.mark.asyncio
async def test_edge_up_at_history_start_is_not_handled(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        await _read_to_start(pilot, screen)
        await wait_until(pilot, lambda: not screen._window_busy, "встал")
        assert screen._window_edge_up() is False


@pytest.mark.asyncio
async def test_tab_lands_on_window_top(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        top = _focusable(screen)[0]
        first = screen._window_first
        await pilot.press("tab")
        assert screen.focused is top
        await wait_until(
            pilot, lambda: screen._window_first < first, "кусок сверху"
        )
        assert screen.focused is top


@pytest.mark.asyncio
async def test_pill_ignores_prepended(tmp_path):
    from gc_tui.widgets.trace_overlays import ScrollEndPill, pill_label

    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        screen = await _preview(pilot)
        first = screen._window_first
        await _user_scroll(pilot, screen, 0)
        await wait_until(
            pilot, lambda: screen._window_first < first, "кусок сверху"
        )
        await pilot.pause()
        pill = screen.query_one(ScrollEndPill)
        assert pill.display is True
        assert pill.label == pill_label(0, enter=False)


# ---- смена сессии ----------------------------------------------------------


def _model(tag: str):
    return build_history(
        _segments(tag), inputs=HistoryInputs(root=None, injected_fields=())
    )


class _WindowHost(TraceWindowMixin, TraceFollowMixin, Screen):
    """Минимальный экран ленты на окне: модель меняется сменой сессии."""

    def __init__(self, model) -> None:
        super().__init__()
        self.model = model

    def compose(self):
        trace = textual_compat.AnchoredScroll(id="trace")
        yield trace
        yield from trace_overlays(trace)

    def on_mount(self) -> None:
        self._window_mount_tail()
        self._watch_trace_follow()
        self._scroll_trace_end()

    def _window_feed(self):
        return self.query_one("#trace")

    def _window_model(self):
        return self.model

    def switch(self, model) -> None:
        self._window_feed().remove_children()
        self.model = model
        self._window_mount_tail()


@pytest.mark.asyncio
async def test_stale_window_result_dropped_after_switch(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        host = _WindowHost(_model("старая"))
        await pilot.app.push_screen(host)
        await pilot.pause(0.3)
        _focusable(host)[0].focus()
        await pilot.pause()
        handled: list[bool] = []

        def edge_then_switch() -> None:
            handled.append(host._window_edge_up())
            host.switch(_model("новая"))
            # Кусок нового окна встаёт: результат прежнего его не трогает.
            host._window_busy = True

        pilot.app.call_later(edge_then_switch)
        await wait_until(pilot, lambda: bool(handled), "переключено")
        fresh_first = host._window_first
        await wait_until(
            pilot, lambda: not textual_compat._PAINT_HOLDS, "отпущено"
        )
        await pilot.pause(0.2)
        replies = _replies(host)
        assert handled == [True]
        assert replies and all(r.startswith("новая") for r in replies)
        assert host._window_first == fresh_first
        assert host._window_busy is True
        assert host.focused in (None, _trace(host))
