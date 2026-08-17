"""PasteInput — вставка из системного буфера (Ctrl+V / правый клик), как CommandTextArea.
Штатный Input читает только внутренний app.clipboard (пуст для внешней копии)."""
import pytest


def _app_with_input():
    from textual.app import App, ComposeResult
    from gc_tui.widgets.paste_input import PasteInput

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PasteInput(id="f")

    return _App()


@pytest.mark.asyncio
async def test_ctrl_v_pastes_from_os_clipboard(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.services import os_ops as os_ops
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "https://pasted/v1/chat/completions")
    from gc_tui.widgets.paste_input import PasteInput

    app = _app_with_input()
    async with app.run_test() as pilot:
        app.query_one("#f", PasteInput).focus()
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert app.query_one("#f", PasteInput).value == "https://pasted/v1/chat/completions"


@pytest.mark.asyncio
async def test_os_clipboard_helper_inserts_and_strips_newlines(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.services import os_ops as os_ops
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "tok-\r\n123")   # многострочный → в одну строку
    from gc_tui.widgets.paste_input import PasteInput

    app = _app_with_input()
    async with app.run_test() as pilot:
        inp = app.query_one("#f", PasteInput)
        inp.focus()
        await pilot.pause()
        assert inp._paste_from_os_clipboard() is True   # общий путь (его же зовёт правый клик)
        assert "\n" not in inp.value and "\r" not in inp.value
        assert "tok-" in inp.value and "123" in inp.value


@pytest.mark.asyncio
async def test_empty_os_clipboard_no_crash(monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.services import os_ops as os_ops
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "")
    from gc_tui.widgets.paste_input import PasteInput

    app = _app_with_input()
    async with app.run_test() as pilot:
        inp = app.query_one("#f", PasteInput)
        inp.focus()
        await pilot.pause()
        assert inp._paste_from_os_clipboard() is False   # пусто → False, не падаем
        assert inp.value == ""


@pytest.mark.asyncio
async def test_paste_replaces_full_selection(monkeypatch):
    """Вставка поверх полного выделения ЗАМЕНЯЕТ значение, а не дописывает к нему."""
    pytest.importorskip("textual")
    from gc_tui.services import os_ops as os_ops
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "http://new/v1/chat/completions")
    from textual.widgets import Input
    from gc_tui.widgets.paste_input import PasteInput

    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PasteInput(value="http://old/v1/chat/completions", id="f")

    app = _App()
    async with app.run_test() as pilot:
        inp = app.query_one("#f", PasteInput)
        inp.focus()
        await pilot.pause()
        from textual.widgets._input import Selection
        inp.selection = Selection(0, len(inp.value))   # выделить всё (как при входе)
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert inp.value == "http://new/v1/chat/completions"   # заменено, не конкатенация


@pytest.mark.asyncio
async def test_paste_without_selection_inserts_at_cursor(monkeypatch):
    """Регресс: без выделения вставка идёт в позицию курсора, как раньше."""
    pytest.importorskip("textual")
    from gc_tui.services import os_ops as os_ops
    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "X")
    from gc_tui.widgets.paste_input import PasteInput
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PasteInput(value="ab", id="f")

    app = _App()
    async with app.run_test() as pilot:
        inp = app.query_one("#f", PasteInput)
        inp.focus()
        await pilot.pause()
        from textual.widgets._input import Selection
        inp.selection = Selection.cursor(len(inp.value))   # курсор в конце, нет выделения
        await pilot.pause()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert inp.value == "abX"
