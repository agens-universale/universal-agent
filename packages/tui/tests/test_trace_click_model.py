"""Клик-модель трейса: клик выделяет строку (как Tab), Ctrl+клик открывает событие.

Мышь и клавиатура симметричны: {клик ↔ Tab} выделение, {Ctrl+клик ↔ Enter} открытие.
Двойной клик события больше НЕ открывает (заменён Ctrl+кликом).
"""

import pytest

pytest.importorskip("textual")

from gc_tui.messages import OpenEventRequest, OpenToolCallDetailRequest
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_row import TraceRow
from gc_tui.widgets.user_reply import UserReplyContainer
from textual import events
from textual.app import App, ComposeResult


def _click(widget, *, ctrl=False, chain=1):
    """Синтетический Click на виджет (координаты внутри виджета)."""
    return events.Click(
        widget=widget,
        x=1,
        y=0,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=False,
        ctrl=ctrl,
        chain=chain,
    )


def test_focus_on_click_not_disabled():
    """Контракт: FOCUS_ON_CLICK не переопределён (True) — Textual фокусирует
    строку уже на MouseDown, выделение видно в момент нажатия (в т.ч. при
    Ctrl+клике до открытия экрана события). Явный focus() в on_click — лишь
    идемпотентная страховка, а не носитель поведения."""
    from gc_tui.widgets.block_panel import BlockPanel

    for cls in (TraceRow, ToolCallRow, UserReplyContainer, BlockPanel):
        assert cls.FOCUS_ON_CLICK is True, cls.__name__


# ---- TraceRow ----


@pytest.mark.asyncio
async def test_plain_click_focuses_row_and_does_not_open():
    """Обычный клик: строка получает фокус, OpenEventRequest НЕ постится."""
    received: list[int] = []

    class _App(App):
        # Без AUTO_FOCUS=None Textual сам фокусирует единственный focusable-
        # виджет на экране — тест перестал бы проверять именно клик.
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="x", kind_class="line-agent", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        assert not row.has_focus
        row.on_click(_click(row))
        await pilot.pause()
        assert row.has_focus
        assert received == []


@pytest.mark.asyncio
async def test_ctrl_click_opens_event():
    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="x", kind_class="line-agent", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        click = _click(row, ctrl=True)
        row.on_click(click)
        await pilot.pause()
        assert received == [7]
        # event.stop() обязателен: иначе клик всплывает к TraceScreen.on_click,
        # который дёргает trace.focus() конкурентно с открытием события.
        assert click._stop_propagation


@pytest.mark.asyncio
async def test_ctrl_click_also_focuses_row():
    """Ctrl+клик выделяет строку КАК обычный клик (и открывает событие):
    после выхода из открытого экрана фокус должен остаться на кликнутой
    строке, а не на прошлом выделении."""
    received: list[int] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="a", kind_class="line-agent", event_no=6)
            yield TraceRow(prefix="● ", content="b", kind_class="line-agent", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        first, second = list(pilot.app.query(TraceRow))
        first.focus()
        await pilot.pause()
        second.on_click(_click(second, ctrl=True))
        await pilot.pause()
        assert received == [7]
        assert second.has_focus
        assert not first.has_focus


@pytest.mark.asyncio
async def test_ctrl_click_on_non_focusable_row_opens_without_focus():
    """focusable=False (continuation prose): Ctrl+клик открывает событие,
    но фокус не забирает."""
    received: list[int] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="  ",
                content="continuation",
                kind_class="line-agent line-agent-cont",
                event_no=7,
                focusable=False,
            )

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        row.on_click(_click(row, ctrl=True))
        await pilot.pause()
        assert received == [7]
        assert not row.has_focus


@pytest.mark.asyncio
async def test_double_click_no_longer_opens():
    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="x", kind_class="line-agent", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        row.on_click(_click(row, chain=2))
        await pilot.pause()
        assert received == []


@pytest.mark.asyncio
async def test_click_on_non_focusable_row_does_not_steal_focus():
    """Строка с focusable=False: клик не выделяет её."""

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="  ",
                content="continuation prose",
                kind_class="line-agent line-agent-cont",
                event_no=1,
                focusable=False,
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        row.on_click(_click(row))
        await pilot.pause()
        assert not row.has_focus


# ---- ToolCallRow ----


@pytest.mark.asyncio
async def test_tool_call_row_plain_click_focuses_and_does_not_open():
    received: list[str] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message: OpenToolCallDetailRequest) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        assert not row.has_focus
        row.on_click(_click(row))
        await pilot.pause()
        assert row.has_focus
        assert received == []


@pytest.mark.asyncio
async def test_tool_call_row_ctrl_click_opens_detail():
    received: list[str] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message: OpenToolCallDetailRequest) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        click = _click(row, ctrl=True)
        row.on_click(click)
        await pilot.pause()
        assert received == ["c42"]
        # event.stop() обязателен: иначе клик всплывает к TraceScreen.on_click,
        # который дёргает trace.focus() конкурентно с открытием детального экрана.
        assert click._stop_propagation


@pytest.mark.asyncio
async def test_tool_call_row_ctrl_click_also_focuses():
    """Ctrl+клик по ToolCallRow выделяет строку и открывает детальный экран."""
    received: list[str] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message: OpenToolCallDetailRequest) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.on_click(_click(row, ctrl=True))
        await pilot.pause()
        assert received == ["c42"]
        assert row.has_focus


@pytest.mark.asyncio
async def test_tool_call_row_double_click_no_longer_opens():
    received: list[str] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

        def on_open_tool_call_detail_request(self, message: OpenToolCallDetailRequest) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.on_click(_click(row, chain=2))
        await pilot.pause()
        assert received == []


@pytest.mark.asyncio
async def test_tool_call_row_focus_does_not_mount_preview_widget():
    """Выделение (focus, Tab/клик) — только выделение: никакого inline-раскрытия
    превью-виджета под строкой. Провалиться в событие — Ctrl+клик/Enter."""

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(call_id="c42", tool_name="read_file", args_summary="", event_no=1)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.focus()
        await pilot.pause()
        assert row.has_focus
        assert list(pilot.app.query(".tool-result-preview")) == []


# ---- UserReplyContainer ----


@pytest.mark.asyncio
async def test_user_reply_plain_click_focuses_and_does_not_open():
    received: list[int] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield UserReplyContainer("привет", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(UserReplyContainer)
        assert not row.has_focus
        row.on_click(_click(row))
        await pilot.pause()
        assert row.has_focus
        assert received == []


@pytest.mark.asyncio
async def test_user_reply_ctrl_click_opens_event():
    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield UserReplyContainer("привет", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(UserReplyContainer)
        click = _click(row, ctrl=True)
        row.on_click(click)
        await pilot.pause()
        assert received == [7]
        assert click._stop_propagation


@pytest.mark.asyncio
async def test_user_reply_double_click_no_longer_opens():
    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield UserReplyContainer("привет", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(UserReplyContainer)
        row.on_click(_click(row, chain=2))
        await pilot.pause()
        assert received == []
