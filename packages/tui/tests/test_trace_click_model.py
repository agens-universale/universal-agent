"""Клик-модель трейса: обычный клик строку не выделяет; клик с модификатором
(Ctrl; на Mac — Cmd, он приходит битом `meta`, как Alt) выделяет на нажатии
и открывает событие на отпускании. С клавиатуры: Tab выделяет, Enter открывает.
Двойной клик события не открывает.
"""

import pytest

pytest.importorskip("textual")

from gc_tui.messages import OpenEventRequest, OpenToolCallDetailRequest
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_row import TraceRow
from gc_tui.widgets.user_reply import UserReplyContainer
from textual import events
from textual.app import App, ComposeResult
from gc_testkit.tui_support import tui_settings


def _click(widget, *, ctrl=False, meta=False, chain=1):
    """Синтетический Click на виджет (координаты внутри виджета)."""
    return events.Click(
        widget=widget,
        x=1,
        y=0,
        delta_x=0,
        delta_y=0,
        button=1,
        shift=False,
        meta=meta,
        ctrl=ctrl,
        chain=chain,
    )


def _press(widget, *, ctrl=False, meta=False):
    """Синтетическое нажатие кнопки мыши на виджет."""
    return events.MouseDown(widget, 1, 0, 0, 0, 1, False, meta, ctrl)


def test_rows_do_not_take_focus_on_plain_press():
    """Контракт СТРОК: обычное нажатие строку не выделяет (FOCUS_ON_CLICK
    False) — выделяет только нажатие с модификатором (`on_mouse_down`)."""
    for cls in (TraceRow, ToolCallRow, UserReplyContainer):
        assert cls.FOCUS_ON_CLICK is False, cls.__name__


@pytest.mark.asyncio
@pytest.mark.parametrize("mod", ["ctrl", "meta"])
async def test_modifier_press_selects_before_release(mod):
    """Выделение видно уже в момент нажатия с модификатором — до того, как
    экран события накроет ленту; обычное нажатие не выделяет."""

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="● ", content="x", kind_class="line-agent", event_no=7
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        row.on_mouse_down(_press(row))
        await pilot.pause()
        assert not row.has_focus
        row.on_mouse_down(_press(row, **{mod: True}))
        await pilot.pause()
        assert row.has_focus


@pytest.mark.asyncio
async def test_alt_click_opens_like_ctrl_click():
    """Клик с `meta` (Cmd на Mac, Alt) — тот же жест, что Ctrl+клик: на Mac
    Ctrl+клик терминал считает правым кликом."""
    received: list[int] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="● ", content="x", kind_class="line-agent", event_no=7
            )

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        row.on_click(_click(row, meta=True))
        await pilot.pause()
        assert received == [7]
        assert row.has_focus


def test_focus_on_click_disabled_for_blocks():
    """Контракт БЛОКОВ: FOCUS_ON_CLICK = False — иначе экран фокусировал бы блок
    на MouseDown по КНОПКЕ ❐ и по хвосту разворота, и блок подсвечивался бы
    выделенным там, где выделять нечего. Носитель выделения — focus() в
    on_click, только за клик по телу."""
    from gc_tui.widgets.block_panel import BlockPanel
    from gc_tui.widgets.diff_block import DiffBlock

    for cls in (BlockPanel, DiffBlock):
        assert cls.FOCUS_ON_CLICK is False, cls.__name__


# ---- TraceRow ----


@pytest.mark.asyncio
async def test_plain_click_neither_selects_nor_opens():
    """Обычный клик: строка не выделяется, событие не открывается, клик
    уходит экрану (тот отдаёт фокус ленте — колесо и стрелки скроллят её)."""
    received: list[int] = []

    class _App(App):
        # Без AUTO_FOCUS=None Textual сам фокусирует единственный focusable-
        # виджет на экране — тест перестал бы проверять именно клик.
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="● ", content="x", kind_class="line-agent", event_no=7
            )

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(TraceRow)
        click = _click(row)
        row.on_click(click)
        await pilot.pause()
        assert not row.has_focus
        assert received == []
        assert not click._stop_propagation


@pytest.mark.asyncio
async def test_ctrl_click_opens_event():
    received: list[int] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield TraceRow(
                prefix="● ", content="x", kind_class="line-agent", event_no=7
            )

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
            yield TraceRow(
                prefix="● ", content="a", kind_class="line-agent", event_no=6
            )
            yield TraceRow(
                prefix="● ", content="b", kind_class="line-agent", event_no=7
            )

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
            yield TraceRow(
                prefix="● ", content="x", kind_class="line-agent", event_no=7
            )

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
async def test_tool_call_row_plain_click_neither_selects_nor_opens():
    received: list[str] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

        def on_open_tool_call_detail_request(
            self, message: OpenToolCallDetailRequest
        ) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.on_click(_click(row))
        await pilot.pause()
        assert not row.has_focus
        assert received == []


@pytest.mark.asyncio
async def test_tool_call_row_ctrl_click_opens_detail():
    received: list[str] = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

        def on_open_tool_call_detail_request(
            self, message: OpenToolCallDetailRequest
        ) -> None:
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
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

        def on_open_tool_call_detail_request(
            self, message: OpenToolCallDetailRequest
        ) -> None:
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
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

        def on_open_tool_call_detail_request(
            self, message: OpenToolCallDetailRequest
        ) -> None:
            received.append(message.call_id)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.on_click(_click(row, chain=2))
        await pilot.pause()
        assert received == []


@pytest.mark.asyncio
async def test_tool_call_row_focus_does_not_mount_preview_widget():
    """Выделение (focus, Tab или клик с модификатором) — только выделение:
    никакого inline-раскрытия превью-виджета под строкой. Провалиться в
    событие — отпускание клика с модификатором или Enter."""

    class _App(App):
        def compose(self) -> ComposeResult:
            yield ToolCallRow(
                call_id="c42",
                label="read_file",
                target="",
                event_no=1,
            )

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(ToolCallRow)
        row.focus()
        await pilot.pause()
        assert row.has_focus
        assert list(pilot.app.query(".tool-result-preview")) == []


# ---- UserReplyContainer ----


@pytest.mark.asyncio
async def test_user_reply_plain_click_neither_selects_nor_opens():
    received: list[int] = []

    class _App(App):
        AUTO_FOCUS = None

        def compose(self) -> ComposeResult:
            yield UserReplyContainer("привет", event_no=7)

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            received.append(message.event_no)

    async with _App().run_test() as pilot:
        row = pilot.app.query_one(UserReplyContainer)
        row.on_click(_click(row))
        await pilot.pause()
        assert not row.has_focus
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


@pytest.mark.asyncio
async def test_a_just_sent_reply_gets_its_event_number_on_delivery(tmp_path):
    """Живая реплика монтируется «отправляется» без номера события; сверка с записью
    треда отдаёт ей номер — Ctrl+клик и Enter открывают событие сразу, а не только
    после перезапуска."""
    import json

    from gc_tui.app import TuiApp

    threads = tmp_path / "threads"
    threads.mkdir()
    tid = "e" * 32
    (threads / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")
    (threads / f"meta_cli_{tid}.json").write_text(
        json.dumps({"thread_id": tid, "tui": {}}), encoding="utf-8"
    )
    async with TuiApp(
        threads_dir=str(threads), system="cli", settings=tui_settings({})
    ).run_test() as pilot:
        scr = pilot.app.screen
        scr.attach_active_task(tid)
        await pilot.pause()
        cid = scr._track_pending(
            display_text="привет", wire_content="привет", images=()
        )
        widget = scr._mount_user_reply(
            "привет", state="pending", client_id=cid
        )
        assert widget._open_request() is None  # ещё не доставлена

        scr._render_record(
            {
                "ts": "2026-09-28T10:00:00+00:00",
                "payload": {"id": "u1", "role": "user", "content": "привет"},
            },
            live=True,
        )
        await pilot.pause()

        request = widget._open_request()
        assert isinstance(request, OpenEventRequest)
        assert request.event_no == scr._task_events[-1].no
