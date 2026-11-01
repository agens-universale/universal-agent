"""Явный вызов `Think` (`session_think`): обрезанный объект строки
раскрывается кликом по нему и пробелом — тело из аргументов вызова
(`thoughts`, `critics`, `doubts`, `reflection` абзацами), `− свернуть`
внизу; влезающий объект не раскрывается; Enter — экран деталей."""

from __future__ import annotations

import json

import pytest
from gc_testkit.paths import TUI_PKG
from gc_tui import palette
from gc_tui.messages import OpenToolCallDetailRequest

_THOUGHTS = "слово " * 40 + "итог"


def _row(think_text: str, summary: str):
    from gc_tui.services.trace_render import build_call_row

    return build_call_row(
        call_id="t1",
        wire_name="session_think",
        args_summary=summary,
        level=0,
        think_text=think_text,
    )


def _app(row, opened: list[str] | None = None):
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        CSS_PATH = str(TUI_PKG / "styles.tcss")

        def get_css_variables(self) -> dict:
            variables = super().get_css_variables()
            variables.update(palette.css_variables())
            return variables

        def compose(self):
            yield VerticalScroll(row, id="t")

        def on_open_tool_call_detail_request(
            self, message: OpenToolCallDetailRequest
        ) -> None:
            if opened is not None:
                opened.append(message.call_id)

    return _A()


def _content(row):
    """Объект `Think` — отдельный Static (ховер и клик только по нему)."""
    return row.query_one(".think-target")


def _line(row) -> str:
    return "".join(
        str(row.query_one(f".{name}").render())
        for name in ("trace-content", "think-target", "think-tail")
    )


async def _click_object(pilot, row) -> None:
    await pilot.click(_content(row), offset=(3, 0))
    await pilot.pause()


def test_think_text_joins_nonempty_fields_in_order():
    from gc_tui.services.think_text import think_text

    args = json.dumps(
        {
            "reflection": "г",
            "thoughts": "а",
            "doubts": "в",
            "critics": " ",
        }
    )
    assert think_text("session_think", args) == "а\n\nв\n\nг"
    assert think_text("fs_read_file", args) == ""
    assert think_text("session_think", "не json") == ""


@pytest.mark.asyncio
async def test_click_on_cut_object_expands_full_text():
    from gc_tui.widgets.expandable_text import ExpandableTextBody

    row = _row(_THOUGHTS, " ".join(_THOUGHTS.split()))
    async with _app(row).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        assert "…" in str(_content(row).render())
        assert _line(row).startswith("Think  слово")
        assert _content(row).has_class("trace-expandable")
        body = row.query_one(ExpandableTextBody)
        assert body.display is False
        await _click_object(pilot, row)
        assert body.display is True
        assert str(body.query_one(".expandable-text").render()) == _THOUGHTS
        collapse = body.query_one(".trace-action")
        assert str(collapse.render()) == "− свернуть"
        await pilot.click(collapse)
        await pilot.pause()
        assert body.display is False


@pytest.mark.asyncio
async def test_short_think_only_selects():
    from gc_tui.widgets.expandable_text import ExpandableTextBody

    row = _row("коротко", "коротко")
    async with _app(row).run_test(size=(60, 30)) as pilot:
        await pilot.pause()
        assert not _content(row).has_class("trace-expandable")
        await _click_object(pilot, row)
        assert row.query_one(ExpandableTextBody).display is False
        assert row.has_focus


@pytest.mark.asyncio
async def test_click_on_label_does_not_expand():
    from gc_tui.widgets.expandable_text import ExpandableTextBody

    row = _row(_THOUGHTS, " ".join(_THOUGHTS.split()))
    async with _app(row).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        await pilot.click(row.query_one(".trace-content"), offset=(1, 0))
        await pilot.pause()
        assert row.query_one(ExpandableTextBody).display is False


@pytest.mark.asyncio
async def test_resize_recomputes_expandability():
    row = _row("средний текст мысли", "средний текст мысли")
    async with _app(row).run_test(size=(80, 30)) as pilot:
        await pilot.pause()
        assert not _content(row).has_class("trace-expandable")
        await pilot.resize_terminal(20, 30)
        await pilot.pause()
        assert _content(row).has_class("trace-expandable")


@pytest.mark.asyncio
async def test_space_toggles_and_enter_opens_detail():
    from gc_tui.widgets.expandable_text import ExpandableTextBody

    row = _row(_THOUGHTS, " ".join(_THOUGHTS.split()))
    opened: list[str] = []
    async with _app(row, opened).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        row.focus()
        body = row.query_one(ExpandableTextBody)
        await pilot.press("space")
        await pilot.pause()
        assert body.display is True
        await pilot.press("space")
        await pilot.pause()
        assert body.display is False
        await pilot.press("enter")
        await pilot.pause()
        assert opened == ["t1"]
        assert body.display is False


def test_mapper_puts_full_think_text_on_call_event():
    from gc_tui.services.trace_mapper import _map_function_event

    args = json.dumps({"thoughts": _THOUGHTS, "doubts": "сомнение"})
    event = _map_function_event(
        {
            "type": "function_call",
            "name": "session_think",
            "call_id": "t1",
            "arguments": args,
        },
        no=1,
        ts="",
        mid=None,
    )
    assert event.think_text == f"{_THOUGHTS.strip()}\n\nсомнение"
    assert len(event.args_summary) <= 80


@pytest.mark.asyncio
async def test_object_has_no_own_color_and_hovers_to_ink():
    from gc_tui.palette import INK, SUB_INK

    row = _row(_THOUGHTS, " ".join(_THOUGHTS.split()))
    async with _app(row).run_test(size=(50, 30)) as pilot:
        row.set_result(summary="", outcome="ok")
        await pilot.pause()
        target = _content(row)
        assert not target.render().spans
        assert target.styles.color.hex.lower() == SUB_INK.lower()
        await pilot.hover(target, offset=(3, 0))
        await pilot.pause()
        assert target.styles.color.hex.lower() == INK.lower()


def _regions(row):
    label = row.query_one(".trace-content").region
    target = _content(row).region
    tail = row.query_one(".think-tail").region
    return label, target, tail


@pytest.mark.asyncio
@pytest.mark.parametrize("width", (40, 60, 100))
@pytest.mark.parametrize("kind", ("short", "cut", "tail"))
async def test_first_line_pieces_are_flush_with_right_gutter(width, kind):
    long = kind == "cut"
    text = _THOUGHTS if long else "коротко"
    row = _row(text, " ".join(text.split()))
    async with _app(row).run_test(size=(width, 30)) as pilot:
        await pilot.pause()
        if kind == "tail":
            row.set_result(summary="готово", outcome="ok")
            await pilot.pause()
        label, target, tail = _regions(row)
        assert label.width == len("Think  ")
        assert target.x == label.right
        assert tail.x == target.right
        assert tail.right <= width - 1
        if long:
            assert tail.right == width - 1
