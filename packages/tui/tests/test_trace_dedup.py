"""S2/A1: два подряд user-facing (kind=agent) ряда с равным контентом → второй
схлопнут в компактную метку (exact-match). Покрывает реальный кейс «проза →
тот же turn_finish/intermediate» — все они мапятся в kind=agent с summary."""

import pytest


def _prose_rec(text):
    return {"payload": {"role": "assistant", "content": text}, "seq": 1, "ts": "2026-06-20T00:00:00+00:00"}


@pytest.mark.asyncio
async def test_duplicate_agent_row_collapsed(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records([_prose_rec("Готово: всё сделано."), _prose_rec("Готово: всё сделано.")])
        await pilot.pause()
        rows = " ".join(f"{getattr(r, '_prefix', '')}{getattr(r, '_content', '')}" for r in scr.query(TraceRow))
        assert rows.count("Готово: всё сделано.") == 1  # контент один раз (дубль молча опущен)


@pytest.mark.asyncio
async def test_distinct_agent_rows_both_shown(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.trace_row import TraceRow

    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_records([_prose_rec("Первая мысль."), _prose_rec("Финальный ответ.")])
        await pilot.pause()
        rows = " ".join(f"{getattr(r, '_prefix', '')}{getattr(r, '_content', '')}" for r in scr.query(TraceRow))
        assert "Первая мысль." in rows and "Финальный ответ." in rows
