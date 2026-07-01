import pytest
from textual.app import App
from textual.widgets import Static
from tui.widgets.trace_row import TraceRow


@pytest.mark.asyncio
async def test_set_content_updates_content_static():
    class _App(App):
        def compose(self):
            yield TraceRow(prefix="● ", content="старый", kind_class="line-agent")

    app = _App()
    async with app.run_test():
        row = app.query_one(TraceRow)
        row.set_content("новый текст")
        content = row.query_one(".trace-content", Static)
        assert "новый текст" in str(content.render())
