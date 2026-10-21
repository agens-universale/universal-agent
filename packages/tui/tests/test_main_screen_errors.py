"""Единое правило ошибок (§3.3): одна строка одним Static; цветовые зоны — rich.Text spans
([!]+суть = red, путь = slate, подсказка = deep-dim)."""

import pytest
from gc_tui.palette import DEEP_DIM


def _rgb(hex_: str) -> str:
    """`#RRGGBB` → `rgb(R,G,B)` — формат, в который rich нормализует Style."""
    r, g, b = int(hex_[1:3], 16), int(hex_[3:5], 16), int(hex_[5:7], 16)
    return f"rgb({r},{g},{b})"


def _error_text(trace):
    """Renderable (rich.Text) единственного error-Static в trace."""
    statics = list(trace.query(".line-error"))
    assert len(statics) == 1
    return statics[0].render()


@pytest.mark.asyncio
async def test_command_error_suggestion_is_meta():
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/statuz"
        await pilot.press("enter")
        await pilot.pause()
        trace = pilot.app.query_one("#trace")
        text = _error_text(trace)
        assert "вы имели в виду" in str(text)
        # подсказка — palette.DEEP_DIM
        assert _rgb(DEEP_DIM) in {str(span.style) for span in text.spans}


@pytest.mark.asyncio
async def test_runtime_error_is_standalone_bang_err():
    """`runtime:` без владельца (до P1b) — `!` в колонке `●`, ERR, без `[!]`."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    async with TuiApp().run_test() as pilot:
        pilot.app.last_runner_error = "boom"
        await pilot.pause(0.6)  # тик опроса — 250 мс
        rows = [
            w
            for w in pilot.app.screen.query(TraceRow)
            if w.has_class("line-error")
        ]
        assert [(r._prefix, r._content) for r in rows] == [
            ("! ", "runtime: boom")
        ]
