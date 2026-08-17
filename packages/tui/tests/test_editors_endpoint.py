"""EndpointEditor: изолированный контракт (harness-app)."""
import pytest

pytest.importorskip("textual")

from textual.app import App, ComposeResult


def _harness(editor):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield editor
    return _App()


async def test_endpoint_editor_prefills_and_submits():
    from gc_tui.editors.endpoint import EndpointEditor
    ed = EndpointEditor(current="https://host/v1/chat/completions")
    app = _harness(ed)
    async with app.run_test():
        ok, value = ed.submit()
        assert ok is True
        assert value == "https://host/v1/chat/completions"


async def test_endpoint_editor_rejects_invalid_url_and_shows_error():
    from gc_tui.editors.endpoint import EndpointEditor
    from textual.widgets import Input
    ed = EndpointEditor(current="")
    app = _harness(ed)
    async with app.run_test():
        ed.query_one(Input).value = "не-урл"
        ok, value = ed.submit()
        assert ok is False and value is None
        assert ed.query("#onb-error")          # ошибка показана внутри редактора


async def test_endpoint_editor_empty_input_means_keep_current():
    from gc_tui.editors.endpoint import EndpointEditor
    ed = EndpointEditor(current="")
    app = _harness(ed)
    async with app.run_test():
        ok, value = ed.submit()
        assert ok is True and value is None


async def test_endpoint_editor_no_hint_static_and_full_placeholder():
    """v2: hint-Static убран, полный текст живёт в ghost-placeholder поля."""
    from gc_tui.editors.endpoint import EndpointEditor
    from textual.widgets import Input
    ed = EndpointEditor(current="")
    app = _harness(ed)
    async with app.run_test():
        assert not ed.query(".onb-hint")
        assert ed.query_one(Input).placeholder == "напр. https://host/v1/chat/completions"
