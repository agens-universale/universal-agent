"""kind=summary рендерит маркер-разделитель + блок с очищенной сводкой."""

from gc_testkit.tui_support import tui_settings
import pytest
from gc_tui.models import TraceEvent


@pytest.mark.asyncio
async def test_summary_marker_renders(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=1,
            event_ts_utc_iso="t",
            kind="summary",
            title="summary",
            summary="контекст сжат",
            summary_content="User asked X. Agent did Y.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        markers = [
            w for w in screen.query(TraceRow) if w.has_class("summary-marker")
        ]
        bodies = [
            w for w in screen.query(TraceRow) if w.has_class("summary-body")
        ]
        assert len(markers) == 1
        assert len(bodies) == 1
        # Блок свёрнут по умолчанию — реально скрыт (display:none), не только класс.
        assert bodies[0].has_class("hidden")
        assert bodies[0].display is False


@pytest.mark.asyncio
async def test_summary_body_toggles_on_request(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import SummaryToggleRequest
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=7,
            event_ts_utc_iso="t",
            kind="summary",
            title="summary",
            summary="контекст сжат",
            summary_content="User asked X.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        body = next(
            w for w in screen.query(TraceRow) if w.has_class("summary-body")
        )
        assert body.display is False  # свёрнут
        # Клик по маркеру (или телу) → раскрытие.
        screen.on_summary_toggle_request(SummaryToggleRequest(7))
        await pilot.pause()
        assert body.display is True
        # Повторный клик → сворачивание (toggle).
        screen.on_summary_toggle_request(SummaryToggleRequest(7))
        await pilot.pause()
        assert body.display is False


@pytest.mark.asyncio
async def test_summary_marker_arrow_flips_on_toggle(tmp_path):
    """Стрелка маркера: ▸ когда свёрнут, ▾ когда раскрыт."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import SummaryToggleRequest
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=3,
            event_ts_utc_iso="t",
            kind="summary",
            title="summary",
            summary="контекст сжат",
            summary_content="X.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        marker = next(
            w for w in screen.query(TraceRow) if w.has_class("summary-marker")
        )
        prefix = lambda: str(marker.query_one(".trace-prefix").render())
        assert "▸" in prefix() and "▾" not in prefix()  # свёрнут → вправо
        screen.on_summary_toggle_request(SummaryToggleRequest(3))
        await pilot.pause()
        assert "▾" in prefix(), "раскрыт → стрелка вниз"
        screen.on_summary_toggle_request(SummaryToggleRequest(3))
        await pilot.pause()
        assert "▸" in prefix(), "снова свёрнут → стрелка вправо"


@pytest.mark.asyncio
async def test_open_summary_event_shows_synthesis_not_empty(tmp_path):
    """Провал в событие summary (segments-путь, full_payload={}) показывает
    синтез сводки, а не пустой {}."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens.viewer import ViewerScreen

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=97,
            event_ts_utc_iso="t",
            kind="summary",
            title="summary",
            summary="контекст сжат",
            summary_content="User did X. Agent did Y.",
            full_payload={},  # segments-путь оставляет payload пустым
        )
        screen._task_events.append(ev)
        screen._open_event_by_no(97)
        await pilot.pause()
        vs = pilot.app.screen
        assert isinstance(vs, ViewerScreen)
        assert "User did X" in str(vs._content), (
            f"summary viewer пуст: {vs._content!r}"
        )


@pytest.mark.asyncio
async def test_summary_expand_scrolls_body_into_view(tmp_path):
    """Раскрытие тела сводки подводит его в зону видимости; сворачивание — нет."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import SummaryToggleRequest
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        screen = pilot.app.screen
        ev = TraceEvent(
            no=5,
            event_ts_utc_iso="t",
            kind="summary",
            title="summary",
            summary="контекст сжат",
            summary_content="X.",
            full_payload={},
        )
        screen._render_event(ev)
        await pilot.pause()
        body = next(
            w for w in screen.query(TraceRow) if w.has_class("summary-body")
        )
        calls = []
        body.scroll_visible = lambda *a, **k: calls.append(True)
        screen.on_summary_toggle_request(SummaryToggleRequest(5))  # раскрыть
        await pilot.pause()
        await pilot.pause()
        assert calls, "при раскрытии body должен скроллиться в зону видимости"
        calls.clear()
        screen.on_summary_toggle_request(SummaryToggleRequest(5))  # свернуть
        await pilot.pause()
        await pilot.pause()
        assert not calls, "при сворачивании скролл не нужен"
