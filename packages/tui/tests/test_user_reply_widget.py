"""UserReplyContainer — band-фон + 2px teal кромка слева, `>` на первой строке."""

import pytest


@pytest.mark.asyncio
async def test_user_reply_single_line():
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield UserReplyContainer("привет агент")

    async with _App().run_test() as pilot:
        widget = pilot.app.query_one(UserReplyContainer)
        rendered = str(widget.render())
        assert "привет агент" in rendered
        assert "> " in rendered  # маркер на первой строке
        assert "user-band" in widget.classes  # NEW: класс применён


@pytest.mark.asyncio
async def test_user_reply_multiline_marker_first_line_only():
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield UserReplyContainer("первая строка\nвторая строка\nтретья")

    async with _App().run_test() as pilot:
        widget = pilot.app.query_one(UserReplyContainer)
        rendered = str(widget.render())
        # Маркер только на первой строке
        lines = [l for l in rendered.split("\n") if l.strip()]
        assert lines[0].lstrip().startswith(">")
        for line in lines[1:]:
            assert not line.lstrip().startswith(">")


@pytest.mark.asyncio
async def test_user_reply_brackets_not_interpreted_as_markup():
    """User text с квадратными скобками НЕ должен парситься как Textual markup."""
    pytest.importorskip("textual")
    from gc_tui.widgets.user_reply import UserReplyContainer
    from textual.app import App, ComposeResult

    class _App(App):
        def compose(self) -> ComposeResult:
            yield UserReplyContainer("check line [42] of [bold]file[/bold]")

    async with _App().run_test() as pilot:
        widget = pilot.app.query_one(UserReplyContainer)
        rendered = str(widget.render())
        # Скобки литерально присутствуют, markup-тэги не парсятся
        assert "[42]" in rendered
        assert "[bold]" in rendered
        assert "[/bold]" in rendered
