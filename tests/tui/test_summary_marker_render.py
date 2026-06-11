"""kind=summary рендерит маркер-разделитель + блок с очищенной сводкой."""
import pytest

from tui.models import TraceEvent


@pytest.mark.asyncio
async def test_summary_marker_renders(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=1, event_ts_utc_iso="t", kind="summary", title="summary",
            summary="контекст сжат", summary_content="User asked X. Agent did Y.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        markers = [w for w in screen.query(TraceRow) if w.has_class("summary-marker")]
        bodies = [w for w in screen.query(TraceRow) if w.has_class("summary-body")]
        assert len(markers) == 1
        assert len(bodies) == 1
        # Блок свёрнут по умолчанию — реально скрыт (display:none), не только класс.
        assert bodies[0].has_class("hidden")
        assert bodies[0].display is False


@pytest.mark.asyncio
async def test_summary_body_toggles_on_request(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.trace_row import TraceRow
    from tui.messages import SummaryToggleRequest
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=7, event_ts_utc_iso="t", kind="summary", title="summary",
            summary="контекст сжат", summary_content="User asked X.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        body = next(w for w in screen.query(TraceRow) if w.has_class("summary-body"))
        assert body.display is False  # свёрнут
        # Клик по маркеру (или телу) → раскрытие.
        screen.on_summary_toggle_request(SummaryToggleRequest(7))
        await pilot.pause()
        assert body.display is True
        # Повторный клик → сворачивание (toggle).
        screen.on_summary_toggle_request(SummaryToggleRequest(7))
        await pilot.pause()
        assert body.display is False
