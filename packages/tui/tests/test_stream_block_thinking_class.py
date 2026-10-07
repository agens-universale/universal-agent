import pytest
from gc_tui.widgets.stream_block import StreamBlock
from textual.app import App


@pytest.mark.asyncio
async def test_set_text_toggles_thinking_class():
    class _App(App):
        def compose(self):
            yield StreamBlock()

    app = _App()
    async with app.run_test():
        block = app.query_one(StreamBlock)
        block.set_text("⋮ думаю", thinking=True)
        assert block.has_class("stream-block-think")
        block.set_text("обычный ответ", thinking=False)
        assert not block.has_class("stream-block-think")
