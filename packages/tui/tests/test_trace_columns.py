"""Колонки ленты (спека P3 §2.1, §2.9): глиф события уровня n — в колонке
`●` реплики агента плюс 2n; один источник отступа (`level_prefix`) у живой
ленты и read-only превью; живая строка хода — в колонке `●`."""

from __future__ import annotations

import json

import pytest
from textual.color import Color

from gc_testkit.tui_support import tui_settings
from gc_tui.palette import DIM
from gc_tui.services.trace_render import (
    LEVEL_INDENT,
    NESTED_PROSE_PREFIX,
    NESTED_USER_PREFIX,
    level_prefix,
)
from gc_tui.widgets.todo_block import TodoBlock
from gc_tui.widgets.tool_call_row import ToolCallRow
from gc_tui.widgets.trace_row import TraceRow
from gc_tui.widgets.user_reply import NestedUserReply, UserReplyContainer

_TODO_ARGS = json.dumps(
    {"todos": [{"content": "шаг", "status": "pending"}]}, ensure_ascii=False
)

_PAYLOADS = [
    {"id": "u1", "role": "user", "content": "поправь конфиг"},
    {"id": "a1", "role": "assistant", "content": "Поправлю конфиг."},
    {
        "id": "fct",
        "type": "function_call",
        "name": "session_todo",
        "call_id": "todo1",
        "arguments": _TODO_ARGS,
    },
    {
        "id": "fcr",
        "type": "function_call",
        "name": "fs_read_file",
        "call_id": "read1",
        "arguments": json.dumps({"path": "config.py"}),
    },
    {
        "id": "for",
        "type": "function_call_output",
        "call_id": "read1",
        "output": "x = 1",
    },
    {
        "id": "fcs",
        "type": "function_call",
        "name": "subagent_worker",
        "call_id": "sub1",
        "arguments": json.dumps({"task": "проверь модуль"}),
    },
    {
        "id": "fos",
        "type": "function_call_output",
        "call_id": "sub1",
        "output": "{}",
    },
    {"id": "a2", "role": "assistant", "content": "Готово."},
]


def _side(kind: str, seq: int, payload: dict) -> dict:
    return {
        "parent_call_id": "sub1",
        "kind": kind,
        "seq": seq,
        "payload": payload,
    }


_SIDECAR = [
    _side(
        "intermediate_reply", 1, {"prose": "Смотрю клиент.", "call_id": "i"}
    ),
    _side(
        "call",
        2,
        {
            "type": "function_call",
            "name": "fs_read_file",
            "arguments": json.dumps({"path": "client.py"}),
            "call_id": "child1",
        },
    ),
    _side(
        "result",
        3,
        {"type": "function_call_output", "call_id": "child1", "output": "ok"},
    ),
    _side("user", 4, {"content": "и тесты"}),
    _side(
        "call",
        5,
        {
            "type": "function_call",
            "name": "session_todo",
            "arguments": _TODO_ARGS,
            "call_id": "child_todo",
        },
    ),
]

_SEGMENTS = [
    {
        "index": 0,
        "messages": [{"ts": "", "payload": p} for p in _PAYLOADS],
        "compacted_by": None,
    }
]

# Ожидаемая раскладка обоих путей: (тип виджета, plain префикса).
_EXPECTED = [
    ("UserReplyContainer", ""),
    ("TraceRow", "● "),
    ("TodoBlock", ""),
    ("ToolCallRow", "● "),
    ("ToolCallRow", "● "),
    ("ConsequenceTraceRow", "  ● "),
    ("ToolCallRow", "  ● "),
    ("NestedUserReply", "  › "),
    ("TodoBlock", "  "),
    ("TraceRow", "● "),
]


# Живая лента собирает вызовы в блоки (P4): чтение — заголовок `Explored`
# и член без глифа, и наверху, и у субагента (заголовок на +2). Превью
# идёт через модель блоков отдельным шагом — до него раскладки путей
# расходятся ровно в этом.
_EXPECTED_LIVE = [
    *_EXPECTED[:3],
    ("ToolCallRow", "● "),
    ("ToolCallRow", "  └ "),
    *_EXPECTED[4:6],
    ("ToolCallRow", "  ● "),
    ("ToolCallRow", "    └ "),
    *_EXPECTED[7:],
]


class _Store:
    def read_subagent_trace(self, task_id):  # noqa: ARG002
        return _SIDECAR


def _plain(value) -> str:
    return getattr(value, "plain", None) or str(value)


def _prefix_plain(w) -> str:
    if isinstance(w, ToolCallRow):
        return w._render_prefix().plain
    if isinstance(w, TodoBlock):
        return _plain(w._rows[0]._prefix)  # заголовок чек-листа
    if isinstance(w, TraceRow):
        return _plain(w._prefix)
    return ""


_LAYOUT_TYPES = (ToolCallRow, TraceRow, TodoBlock, UserReplyContainer)


def _layout(trace) -> list[tuple[str, str]]:
    return [
        (type(w).__name__, _prefix_plain(w))
        for w in trace.children
        if isinstance(w, _LAYOUT_TYPES) and w.id != "brand-block"
    ]


def _glyph_x(prefix_widget, plain: str) -> int:
    return prefix_widget.content_region.x + len(plain) - len(plain.lstrip())


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


async def _live_trace(pilot):
    from textual.containers import VerticalScroll

    scr = pilot.app.screen
    scr._store = _Store()
    scr._render_segments(_SEGMENTS)
    scr._merge_sidecar_nested("t")
    await pilot.pause()
    return scr.query_one("#trace", VerticalScroll)


def test_level_prefix_two_spaces_per_level():
    assert LEVEL_INDENT == "  "
    assert level_prefix(0) == ""
    assert level_prefix(1) == "  "
    assert level_prefix(2) == "    "


def test_nested_prefixes_come_from_level():
    assert NESTED_PROSE_PREFIX == "  ● "
    assert NESTED_USER_PREFIX == "  › "


@pytest.mark.parametrize("level", [0, 1, 2])
def test_tool_call_row_prefix_is_level_plus_glyph(level):
    row = ToolCallRow(
        call_id="c", label="fs_read_file", target="", level=level
    )
    row.set_result(summary="", outcome="ok")
    assert row._render_prefix().plain == level_prefix(level) + "● "
    assert row.has_class("nested-tool") == (level > 0)


@pytest.mark.asyncio
async def test_live_and_preview_share_layout(tmp_path):
    """Одни и те же записи живой лентой и превью — одна раскладка (кроме
    блоков вызовов живой ленты, `_EXPECTED_LIVE`)."""
    from textual.app import App
    from textual.containers import VerticalScroll

    from gc_tui.services.trace_render import (
        merge_nested_readonly,
        render_segments_readonly,
    )

    async with _app(tmp_path).run_test(size=(120, 40)) as pilot:
        live = _layout(await _live_trace(pilot))

    class _Preview(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            t = self.query_one("#t", VerticalScroll)
            calls = render_segments_readonly(t, _SEGMENTS)
            merge_nested_readonly(t, _SIDECAR, calls)

    async with _Preview().run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        preview = _layout(pilot.app.query_one("#t", VerticalScroll))

    assert live == _EXPECTED_LIVE
    assert preview == _EXPECTED


@pytest.mark.asyncio
async def test_live_glyph_columns_step_two_per_level(tmp_path):
    """Глиф вызова уровня 0 — в колонке `●`, уровня 1 и вложенной реплики —
    на +2; вложенная реплика субагента — серый `●`. У члена блока глифа
    нет (P4 §3.1) — его колонку держит заголовок. Реплика субагента — за
    окном `Agent` (4 события): окно раскрыто."""
    async with _app(tmp_path).run_test(size=(120, 40)) as pilot:
        trace = await _live_trace(pilot)
        pilot.app.screen._block_views["sub1"].toggle("agent")
        await pilot.pause()
        agent = next(w for w in trace.children if isinstance(w, TraceRow))
        col = _glyph_x(agent.query_one(".trace-prefix"), "● ")
        rows = [r for r in trace.query(ToolCallRow) if r.mode != "member"]
        top = [r for r in rows if r._level == 0]
        nested = [r for r in rows if r._level == 1]
        for row in top + nested:
            got = _glyph_x(
                row.query_one(".trace-prefix"), row._render_prefix().plain
            )
            assert got == col + 2 * row._level
        prose = next(w for w in trace.children if w.has_class("nested-prose"))
        prose_prefix = prose.query_one(".trace-prefix")
        assert _glyph_x(prose_prefix, NESTED_PROSE_PREFIX) == col + 2
        assert prose_prefix.styles.color == Color.parse(DIM)


@pytest.mark.asyncio
async def test_turn_line_sits_in_agent_bullet_column(tmp_path):
    """Место строки хода (§2.9): глиф индикатора в #indicator-bar — в той же
    колонке, что `●` реплики агента в #trace."""
    from gc_tui.widgets.thinking_indicator import ThinkingIndicator

    async with _app(tmp_path).run_test(size=(120, 40)) as pilot:
        trace = await _live_trace(pilot)
        scr = pilot.app.screen
        scr._bar_wanted = lambda: True  # идёт работа: бар стоит (S10 §3.3)
        scr._show_thinking_indicator(state="thinking")
        await pilot.pause()
        bar = pilot.app.query_one("#indicator-bar")
        indicator = bar.query_one(ThinkingIndicator)
        agent = next(w for w in trace.children if isinstance(w, TraceRow))
        col = _glyph_x(agent.query_one(".trace-prefix"), "● ")
        assert indicator.content_region.x == col


@pytest.mark.asyncio
async def test_turn_line_glyph_in_agent_bullet_column(tmp_path):
    """Строка итога хода в ленте (S10): её глиф — в колонке `●` реплики
    агента."""
    from gc_tui.services.trace_render import build_turn_line

    async with _app(tmp_path).run_test(size=(120, 40)) as pilot:
        trace = await _live_trace(pilot)
        line = build_turn_line(word="completed")
        line.freshen((1, 2, 3), None)
        pilot.app.screen._trace_mount(line)
        await pilot.pause()
        agent = next(w for w in trace.children if isinstance(w, TraceRow))
        col = _glyph_x(agent.query_one(".trace-prefix"), "● ")
        assert line.content_region.x == col


def _styled(app_cls):
    """Хост превью с настоящими стилями приложения (как `tui_app_factory`)."""
    from gc_testkit.paths import TUI_PKG
    from gc_tui import palette

    class _Styled(app_cls):
        CSS_PATH = str(TUI_PKG / "styles.tcss")

        def get_css_variables(self) -> dict:
            variables = super().get_css_variables()
            variables.update(palette.css_variables())
            return variables

    return _Styled


@pytest.mark.asyncio
async def test_blank_line_before_every_call_on_both_paths(tmp_path):
    """Интервал (§2.1): пустая строка перед каждым вызовом — верхним и
    вложенным, подряд идущим тоже; класса `tool-group-start` нет ни вживую,
    ни в превью. Вживую (P4 §3.1, §7) — перед каждым блоком верхнего
    уровня: у члена блока и внутри блока субагента пустых строк нет."""
    from textual.app import App
    from textual.containers import VerticalScroll

    from gc_tui.services.trace_render import (
        merge_nested_readonly,
        render_segments_readonly,
    )

    async with _app(tmp_path).run_test(size=(120, 40)) as pilot:
        trace = await _live_trace(pilot)
        rows = [r for r in trace.query(ToolCallRow) if r.mode != "member"]
        assert {r._level for r in rows} == {0, 1}
        assert [r.styles.margin.top for r in rows] == [
            0 if r._level else 1 for r in rows
        ]
        members = [r for r in trace.query(ToolCallRow) if r.mode == "member"]
        assert [r.styles.margin.top for r in members] == [0, 0]
        assert not trace.query(".tool-group-start")
        lines = [
            "".join(seg.text for seg in strip).rstrip()
            for strip in pilot.app.screen._compositor.render_strips()
        ]
        read = next(i for i, s in enumerate(lines) if "Read  " in s)
        sub = next(i for i, s in enumerate(lines) if "Agent  " in s)
        assert sub == read + 2 and lines[read + 1] == ""

    class _Preview(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            t = self.query_one("#t", VerticalScroll)
            calls = render_segments_readonly(t, _SEGMENTS)
            merge_nested_readonly(t, _SIDECAR, calls)

    async with _styled(_Preview)().run_test(size=(120, 40)) as pilot:
        await pilot.pause()
        trace = pilot.app.query_one("#t", VerticalScroll)
        rows = list(trace.query(ToolCallRow))
        assert {r._level for r in rows} == {0, 1}
        assert [r.styles.margin.top for r in rows] == [1] * len(rows)
        assert not trace.query(".tool-group-start")
