"""Read-only превью рендерит прозу так же, как трейс (через build_block_widgets)."""

import pytest


@pytest.mark.asyncio
async def test_preview_multiparagraph_prose_single_row(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.services.trace_render import render_thread_readonly
    from gc_tui.widgets.trace_row import TraceRow
    from textual.app import App
    from textual.containers import VerticalScroll

    rec = {
        "payload": {
            "id": "a1",
            "role": "assistant",
            "content": "абзац1\n\nабзац2",
        },
        "ts": "",
    }

    class _App(App):
        def compose(self):
            yield VerticalScroll(id="m")

    app = _App()
    async with app.run_test() as pilot:
        target = app.query_one("#m", VerticalScroll)
        render_thread_readonly(target, [rec])
        await pilot.pause()
        rows = list(target.query(TraceRow))
        prose_rows = [r for r in rows if "абзац" in str(r._content.plain)]
        assert len(prose_rows) == 1  # один прогон, пустая строка внутри
        assert "\n\n" in str(prose_rows[0]._content.plain)
