"""Хелпер сравнения и обновления Static."""

import pytest
from gc_tui.widgets.static_content import (
    same_content,
    same_size,
    text_key,
    update_if_changed,
)
from rich.console import Group
from rich.markdown import Markdown
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


def test_same_size_digit_change():
    assert same_size(Text("· 5с"), Text("· 6с"), markup=True) is True
    assert same_size("· 5с", "· 6с", markup=False) is True


def test_size_measured_by_chunks_not_code_points():
    """Ширина — по кускам целиком, как у Rich и Textual: графема из
    нескольких кодовых точек занимает свою ширину, а не их сумму."""
    assert same_size(Text("❤️ a"), Text("b a"), markup=False) is False
    assert same_size("❤️ a", "b a", markup=False) is False
    assert same_size(Text("👨‍👩‍👧 a"), Text("abcdef a"), markup=True) is False
    assert same_size(Text("👨‍👩‍👧 a"), Text("ab a"), markup=True) is True


def test_size_changes_on_width():
    assert same_size(Text("· 9с"), Text("· 10с"), markup=True) is False


def test_size_changes_on_line_count():
    assert same_size(Text("ab\ncd"), Text("ab\ncd\nef"), markup=True) is (
        False
    )


def test_size_changes_on_whitespace_shift():
    assert same_size(Text("a bc"), Text("ab c"), markup=True) is False


def test_size_changes_on_no_wrap_or_overflow():
    assert (
        same_size(Text("ab", no_wrap=True), Text("cd"), markup=True) is False
    )
    assert (
        same_size(Text("ab", overflow="ellipsis"), Text("cd"), markup=True)
        is False
    )
    assert (
        same_size(Text("ab", justify="right"), Text("cd"), markup=True)
        is False
    )
    assert same_size(Text("ab", tab_size=4), Text("cd"), markup=True) is False


def test_tab_forces_layout():
    assert same_size(Text("a\tb"), Text("c\td"), markup=True) is False
    assert same_size("a\tb", "c\td", markup=False) is False


def test_non_text_forces_layout():
    group = Group(Text("a"))
    assert same_size(group, group, markup=False) is False
    markdown = Markdown("a")
    assert same_size(markdown, markdown, markup=False) is False
    assert same_size("a", Text("b"), markup=False) is False


def test_markup_str_forces_layout():
    assert same_size("a", "b", markup=True) is False


@pytest.mark.asyncio
async def test_update_if_changed_same_size_no_layout(monkeypatch):
    class _App(App):
        def compose(self) -> ComposeResult:
            yield Static(id="s")

    calls: list[bool] = []
    original = Static.update

    def spy(self, content="", *, layout=True):
        calls.append(layout)
        original(self, content, layout=layout)

    async with _App().run_test() as pilot:
        s = pilot.app.query_one("#s", Static)
        s.update(Text("· 5с"))
        monkeypatch.setattr(Static, "update", spy)
        update_if_changed(s, Text("· 6с"))
        update_if_changed(s, Text("· 10с"))
        update_if_changed(s, Text("· 10с"))
        assert calls == [False, True]
        assert s.content.plain == "· 10с"
