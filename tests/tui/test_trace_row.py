"""TraceRow — двух-колоночный layout (prefix + content) для hanging indent."""
import pytest


@pytest.mark.asyncio
async def test_trace_row_composes_horizontal_with_two_statics():
    """DOM: TraceRow содержит Horizontal с двумя Static (.trace-prefix + .trace-content)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from textual.widgets import Static
    from tui.widgets.trace_row import TraceRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="hello world", kind_class="line-agent")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        statics = list(row.query(Static))
        assert len(statics) == 2
        prefix_static, content_static = statics
        assert "trace-prefix" in prefix_static.classes
        assert "trace-content" in content_static.classes


@pytest.mark.asyncio
async def test_trace_row_kind_class_lands_on_widget():
    """kind_class попадает ТОЛЬКО на сам TraceRow (не на content-Static).

    Дубль на content-Static вызывал двойное срабатывание `.line-* { margin-top: 1 }`:
    margin двигал и ряд (TraceRow), и content внутри Horizontal — prefix оставался
    на верхней строке, content уезжал вниз → визуальный разрыв между ● и текстом.
    """
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.trace_row import TraceRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="[policy] ", content="test", kind_class="line-meta")

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        assert row.has_class("line-meta")
        content = row.query_one(".trace-content")
        assert "line-meta" not in content.classes


@pytest.mark.asyncio
async def test_trace_row_double_click_emits_open_event_request():
    """Двойной клик → OpenEventRequest (parity с AgentReplyLine)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from textual import events
    from tui.widgets.trace_row import TraceRow
    from tui.messages import OpenEventRequest

    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="● ", content="x", kind_class="line-agent", event_no=42)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        # Direct on_click() call (как в test_double_click_open) — иначе App
        # auto-forwards bubbled Click через screen._forward_event и обработчик
        # срабатывает дважды (артефакт тестового pattern, не production).
        click = events.Click(
            widget=row, x=0, y=0, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False, chain=2,
            screen_x=0, screen_y=0, style=None,
        )
        row.on_click(click)
        await pilot.pause()
        assert received == [42]


@pytest.mark.asyncio
async def test_trace_row_event_no_zero_does_not_emit():
    """event_no=0 → клик не открывает payload (нет события)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from textual import events
    from tui.widgets.trace_row import TraceRow
    from tui.messages import OpenEventRequest

    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="[stopped]", content="", kind_class="line-meta", event_no=0)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        click = events.Click(
            widget=row, x=0, y=0, delta_x=0, delta_y=0,
            button=1, shift=False, meta=False, ctrl=False, chain=2,
            screen_x=0, screen_y=0, style=None,
        )
        row.on_click(click)
        await pilot.pause()
        assert received == []


@pytest.mark.asyncio
async def test_trace_row_focusable_default_true():
    """Без явного focusable — TraceRow.can_focus == True (дефолт-поведение для
    самостоятельных одно-строчных событий: framework `[meta]`, `? think`, ...)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.trace_row import TraceRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="[skill] ", content="x",
                           kind_class="line-meta", event_no=1)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        assert row.can_focus is True


@pytest.mark.asyncio
async def test_trace_row_focusable_false_opts_out():
    """focusable=False → can_focus == False (для continuation prose, `* note:`)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.trace_row import TraceRow

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(prefix="  ", content="continuation prose",
                           kind_class="line-agent line-agent-cont",
                           event_no=1, focusable=False)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        assert row.can_focus is False


@pytest.mark.asyncio
async def test_agent_reply_line_focusable_default_false():
    """Дефолт AgentReplyLine.can_focus == False (Static-наследник, обычно continuation)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine("x", event_no=1)

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        assert w.can_focus is False


@pytest.mark.asyncio
async def test_agent_reply_line_focusable_true_opt_in():
    """focusable=True → can_focus == True (для edge-case `только code-block`-событий)."""
    pytest.importorskip("textual")
    from textual.app import App, ComposeResult
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine("x", event_no=1, focusable=True)

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        assert w.can_focus is True
