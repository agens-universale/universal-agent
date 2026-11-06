"""Рендер intermediate_reply (серый ● — спека P3 §10, п. 4) и error_msg
(`!` красным, без владельца) в трейсе."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.models import TraceEvent


def _intermediate_event() -> TraceEvent:
    return TraceEvent(
        no=1,
        event_ts_utc_iso="t",
        kind="agent",
        title="",
        summary="нашёл проблему, чиню...",
        full_payload={},
        tool_name="intermediate_reply",
    )


_INTERMEDIATE_CALL = {
    "type": "function_call",
    "id": "fc1",
    "call_id": "c1",
    "name": "intermediate_reply",
    "arguments": '{"answer": "нашёл проблему, чиню..."}',
}
_SEGMENTS = [
    {
        "index": 0,
        "messages": [{"ts": "", "payload": _INTERMEDIATE_CALL}],
        "compacted_by": None,
    }
]


def _assert_grey_bullet(rows) -> None:
    """Промежуточная реплика — `● ` уровня 0, глиф серым (DIM)."""
    from textual.color import Color

    from gc_tui.palette import DIM

    assert len(rows) == 1
    assert rows[0]._prefix == "● "
    prefix = rows[0].query_one(".trace-prefix")
    assert prefix.styles.color == Color.parse(DIM)


@pytest.mark.asyncio
async def test_intermediate_reply_renders_as_grey_bullet_live(tmp_path):
    pytest.importorskip("textual")
    from textual.color import Color

    from gc_tui.app import TuiApp
    from gc_tui.palette import DIM
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        screen._render_event(_intermediate_event())
        screen._render_event(
            TraceEvent(
                no=2,
                event_ts_utc_iso="t",
                kind="agent",
                title="",
                summary="готово",
                full_payload={},
            )
        )
        await pilot.pause()
        rows = list(screen.query(TraceRow))
        _assert_grey_bullet(
            [w for w in rows if w.has_class("line-intermediate")]
        )
        final = next(w for w in rows if w.has_class("line-agent"))
        assert final._prefix == "● "
        glyph = final.query_one(".trace-prefix").styles.color
        assert glyph != Color.parse(DIM)


@pytest.mark.asyncio
async def test_intermediate_reply_renders_as_grey_bullet_in_preview():
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll

    from gc_testkit.paths import TUI_PKG
    from gc_tui import palette
    from gc_tui.services.trace_render import render_segments_readonly
    from gc_tui.widgets.trace_row import TraceRow

    class _Preview(App):
        CSS_PATH = str(TUI_PKG / "styles.tcss")

        def get_css_variables(self) -> dict:
            variables = super().get_css_variables()
            variables.update(palette.css_variables())
            return variables

        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            render_segments_readonly(
                self.query_one("#t", VerticalScroll), _SEGMENTS
            )

    async with _Preview().run_test() as pilot:
        await pilot.pause()
        rows = [
            w
            for w in pilot.app.query(TraceRow)
            if w.has_class("line-intermediate")
        ]
        _assert_grey_bullet(rows)


@pytest.mark.asyncio
async def test_error_msg_renders_red_bang(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="error_msg",
            title="!",
            summary="HTTP 500: backend unavailable.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        rows = [w for w in screen.query(TraceRow) if w.has_class("line-error")]
        assert [r._prefix for r in rows] == ["! "]


@pytest.mark.asyncio
async def test_error_result_without_call_row_is_standalone_bang(tmp_path):
    """Ошибка результата, чьей строки вызова нет, — `! …` ERR, без скобок."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=2,
            event_ts_utc_iso="t",
            kind="error",
            title="",
            summary="ENOENT",
            full_payload={},
            call_id="нет-такого",
        )
        screen._render_event(ev)
        await pilot.pause()
        rows = [w for w in screen.query(TraceRow) if w.has_class("line-error")]
        assert [(r._prefix, r._content) for r in rows] == [("! ", "ENOENT")]
