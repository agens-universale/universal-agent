"""Хелпер сравнения и обновления Static."""

import pytest
from gc_tui.widgets.static_content import (
    same_content,
    text_key,
    update_if_changed,
)
from rich.text import Text
from textual.app import App, ComposeResult
from textual.widgets import Static


def test_same_str():
    assert same_content("a", "a") is True
    assert same_content("a", "b") is False


def test_text_spans_and_plain():
    a = Text("x")
    a.stylize("red", 0, 1)
    b = Text("x")
    b.stylize("red", 0, 1)
    c = Text("x")
    c.stylize("blue", 0, 1)
    assert same_content(a, b) is True
    assert same_content(a, c) is False


def test_text_base_style_differs():
    assert (
        same_content(Text("x", style="red"), Text("x", style="blue")) is False
    )


def test_text_justify_differs():
    assert (
        same_content(Text("x", justify="left"), Text("x", justify="right"))
        is False
    )


def test_text_key_by_content_not_identity():
    assert text_key(None) is None
    assert text_key(Text("x", style="red")) == text_key(Text("x", style="red"))
    assert text_key(Text("x", style="red")) != text_key(
        Text("x", style="blue")
    )


def test_mixed_and_unknown_types_count_as_changed():
    assert same_content("x", Text("x")) is False
    obj = object()
    assert same_content(obj, obj) is False


@pytest.mark.asyncio
async def test_update_if_changed_skips_equal_and_applies_new():
    class _App(App):
        def compose(self) -> ComposeResult:
            yield Static(id="s")

    async with _App().run_test() as pilot:
        s = pilot.app.query_one("#s", Static)
        s.update(Text("a"))
        first = s.content
        update_if_changed(s, Text("a"))
        assert s.content is first
        update_if_changed(s, Text("b"))
        assert s.content.plain == "b"
