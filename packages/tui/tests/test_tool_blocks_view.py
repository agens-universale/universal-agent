"""Исполнитель решений модели блоков `gc_tui.widgets.tool_blocks_view`.

Мини-приложение с настоящими стилями и `#trace`; хост монтирования —
`trace.mount`, `BlockToggle` маршрутизирует `BlockViews`. Отрисованная лента
проверяется сеткой колонок `assert_trace_grid` (спека P4 §3.1).
"""

from __future__ import annotations

import pytest
from gc_core.agent_edits.journal import EditRecord
from gc_core.constants.tool_display import DisplayCategory as C
from gc_testkit.paths import TUI_PKG
from gc_testkit.trace_grid import (
    assert_trace_grid,
    record_frames,
    trace_rows,
)
from rich.style import Style
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll

from gc_tui import palette
from gc_tui.messages import BlockToggle
from gc_tui.models import CommandOutput, TraceEvent
from gc_tui.services.diff_render import diff_line_count
from gc_tui.services.tool_blocks import BlockEdit, CallTimers, ToolBlocks
from gc_tui.widgets import tool_call_row
from gc_tui.widgets.diff_block import DiffBlock
from gc_tui.widgets.tool_blocks_view import (
    BlockAction,
    BlockViews,
    ToolBlocksView,
)
from gc_tui.widgets.tool_call_row import ToolCallRow

ORIGIN = 1


class _Host:
    def __init__(self, trace: VerticalScroll) -> None:
        self._trace = trace

    def mount(self, widget, *, before=None, after=None) -> None:
        self._trace.mount(widget, before=before, after=after)


class _App(App):
    CSS_PATH = str(TUI_PKG / "styles.tcss")

    def __init__(self) -> None:
        super().__init__()
        self.views = BlockViews()
        self.timers = CallTimers()

    def get_css_variables(self) -> dict:
        variables = super().get_css_variables()
        variables.update(palette.css_variables())
        return variables

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="trace")

    def view(self, level: str = "", depth: int = 0) -> ToolBlocksView:
        trace = self.query_one("#trace", VerticalScroll)
        model = ToolBlocks(self.timers, diff_preview=3)
        view = ToolBlocksView(_Host(trace), model, level=level, depth=depth)
        self.views[level] = view
        return view

    def on_block_toggle(self, message: BlockToggle) -> None:
        self.views.route(message)


def _row(call_id: str, label: str = "Read", target: str = "a.py", depth=0):
    return ToolCallRow(
        call_id=call_id, label=label, target=f"{target}", level=depth
    )


def _ev(call_id: str, summary: str = "", outcome=None) -> TraceEvent:
    return TraceEvent(
        no=0,
        event_ts_utc_iso="",
        kind="error" if outcome not in (None, "ok") else "result",
        title="",
        summary=summary,
        full_payload={},
        call_id=call_id,
        outcome=outcome,
    )


def _read(view, key, *, done=True, summary="12 строк", outcome=None):
    row = _row(key, target=f"{key}.py", depth=view.depth)
    view.add_call(row, C.READ, waiting=False)
    view.started(row)
    if done:
        view.result(row, _ev(key, summary, outcome), None)
    return row


def _plain(rows) -> list[str]:
    return [r.plain.rstrip() for r in rows]


def _colour(row, col: int) -> str:
    colour = ""
    for span in row.spans:
        if span.start <= col < span.end:
            style = span.style
            if isinstance(style, str):
                style = Style.parse(style)
            if style is not None and style.color is not None:
                colour = style.color.get_truecolor().hex.lower()
    return colour


def _line(rows, needle: str):
    return next(r for r in rows if needle in r.plain)


@pytest.fixture
def steady_blink(monkeypatch):
    """Живой глиф виден в любом кадре — снимок ленты не ловит паузу."""
    monkeypatch.setattr(tool_call_row, "blink_on", lambda frame: True)


@pytest.mark.asyncio
async def test_five_reads_header_window_and_phrase():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        rows = [_read(view, f"c{i}") for i in range(5)]
        await pilot.pause()
        headers = [w for w in app.query(ToolCallRow) if w.mode == "header"]
        assert len(headers) == 1
        phrases = list(app.query(BlockAction))
        assert [p.phrase for p in phrases] == ["… ещё 2"]
        hidden = [r for r in rows if not r.display]
        assert hidden == rows[:2]
        lines = _plain(trace_rows(app))
        assert " ● Explored" in lines
        assert "   └ … ещё 2" in lines


@pytest.mark.asyncio
async def test_grid_live_and_done(steady_blink):
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        for i in range(4):
            _read(view, f"c{i}")
        last = _read(view, "c4", done=False)
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        head = _line(rows, "Exploring")
        assert _colour(head, ORIGIN) == palette.RUN_COLOR.lower()
        member = _line(rows, "c3.py")
        member_colour = _colour(member, member.plain.index("c3.py"))
        live = _line(rows, "c4.py")
        assert _colour(live, live.plain.index("c4.py")) == (
            palette.INK.lower()
        )
        view.result(last, _ev("c4", "1 строка"), None)
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        head = _line(rows, "Explored")
        assert _colour(head, ORIGIN) == palette.OK.lower()
        member = _line(rows, "c3.py")
        assert _colour(member, member.plain.index("c3.py")) == member_colour
        assert member_colour == palette.MUTED.lower()


@pytest.mark.asyncio
async def test_failed_member_reason_and_timer():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        _read(view, "c0")
        failing = _read(view, "e", done=False)
        # Исполнялся 2 с: застывший таймер есть только от секунды.
        failing._start_monotonic -= 2
        view.result(failing, _ev("e", "не найден", "error"), None)
        _read(view, "r", summary="отказ: защита путей", outcome="refused")
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        reason = _line(rows, "не найден")
        col = reason.plain.index("не найден")
        # `└` члена — под началом его текста (колонка 4), причина — на +2.
        assert col == ORIGIN + 6
        assert _colour(reason, col) == palette.ERR.lower()
        failed = _line(rows, "e.py")
        assert _colour(failed, failed.plain.index("e.py")) == (
            palette.MUTED.lower()
        )
        assert failed.plain.rstrip().endswith("· 2с")
        assert _colour(failed, len(failed.plain.rstrip()) - 1) == (
            palette.DIM.lower()
        )
        refused = _line(rows, "r.py")
        assert "·" not in refused.plain
        cause = _line(rows, "отказ: защита")
        assert _colour(cause, cause.plain.index("отказ")) == (
            palette.REFUSED.lower()
        )
        head = _line(rows, "Explored")
        assert "· 1 ошибка · 1 отказ" in head.plain


def _record(call_id: str, lines: int, *, context: bool = False) -> EditRecord:
    """``lines`` добавленных строк; ``context`` — с тремя строками
    контекста сверху, как в журнале (`hunk_context_lines: 3`)."""
    body = "".join(f"+line {i}\n" for i in range(lines))
    head = f"@@ -0,0 +1,{lines} @@\n"
    if context:
        head = f"@@ -1,3 +1,{lines + 3} @@\n ctx 1\n \n ctx 3\n"
    return EditRecord(
        path=f"{call_id}.py",
        turn_id="t1",
        ts=0.0,
        call_id=call_id,
        op="edit",
        hunks=head + body,
        add=lines,
        del_=0,
        before_sha256=None,
        after_sha256=None,
        flags=[],
    )


def _edit_of(record: EditRecord) -> BlockEdit:
    return BlockEdit(
        key=record.call_id,
        path=record.path,
        verb="Edited",
        add=record.add,
        rem=record.del_,
        diff_lines=diff_line_count(record.hunks),
        size_bytes=None,
        has_record=True,
        copy_text="diff",
        record=record,
    )


def _apply_edit(view, key: str, lines: int, *, context: bool = False):
    record = _record(key, lines, context=context)
    row = _row(key, label="Edit", target=record.path, depth=view.depth)
    view.add_call(row, C.EDIT, waiting=False)
    view.started(row)
    view.result(row, _ev(key, "готово"), _edit_of(record))
    return row


@pytest.mark.asyncio
async def test_group_preview_budget_then_full_diff():
    """Блок `Edited 2 файла`, бюджет 3 строки на файл: у `a` (1 изменённая
    и 3 контекста) — изменённая и два ближайших контекста; у `b` (5
    изменённых) — первые 3 без контекста. Под каждым диффом своя фраза —
    скрытые строки файла; файл раскрывается сам, заголовок — весь блок.
    Сетка колонок — во всех видах."""
    app = _App()
    async with app.run_test(size=(80, 40)) as pilot:
        view = app.view()
        _apply_edit(view, "a", 1, context=True)
        _apply_edit(view, "b", 5, context=True)
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        lines = [line for line in _plain(rows) if line]
        assert sum("ctx" in line for line in lines) == 1
        assert sum("+line" in line for line in lines) == 4
        # a: скрыт дальний контекст; b: 3 контекста и 2 изменённые сверх 3
        # — каждая фраза под диффом своего файла.
        phrases = [i for i, line in enumerate(lines) if "+ ещё" in line]
        b_row = next(i for i, line in enumerate(lines) if "b.py" in line)
        assert [lines[i] for i in phrases] == [
            "     + ещё 1 строка",
            "     + ещё 5 строк",
        ]
        assert phrases[0] < b_row < phrases[1] == len(lines) - 1
        view.toggle("a")
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        lines = _plain(rows)
        # Раскрыт только a: его две строки `ctx` (третья — пустая).
        assert sum("ctx" in line for line in lines) == 2
        assert sum("+line" in line for line in lines) == 4
        assert "     − свернуть" in lines
        view.toggle("block:0")
        await pilot.pause()
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        lines = _plain(rows)
        assert sum("ctx" in line for line in lines) == 4
        assert sum("+line" in line for line in lines) == 6
        assert sum(line == "     − свернуть" for line in lines) == 2


@pytest.mark.asyncio
async def test_single_to_block_keeps_same_diffblock():
    app = _App()
    async with app.run_test(size=(80, 40)) as pilot:
        view = app.view()
        _apply_edit(view, "a", 8)
        await pilot.pause()
        diff = app.query_one(DiffBlock)
        assert diff.has_class("diff-single")
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        assert _plain(rows)[1].startswith(" ● Edited a.py (+8 −0)  ")
        _apply_edit(view, "b", 8)
        await pilot.pause()
        diffs = list(app.query(DiffBlock))
        assert diffs[0] is diff
        assert diff.has_class("diff-member")
        rows = trace_rows(app)
        assert_trace_grid(rows, origin=ORIGIN)
        lines = _plain(rows)
        assert " ● Edited 2 файла (+16 −0)" in lines
        assert any(line.startswith("   └ a.py (+8 −0)  ") for line in lines)
        phrases = [line for line in lines if "+ ещё" in line]
        assert phrases == ["     + ещё 5 строк"] * 2
        assert [line for line in lines if line][-1] == "     + ещё 5 строк"


def _phrase_of(app, key: str) -> BlockAction:
    return next(p for p in app.query(BlockAction) if p.key == key)


def _shown_lines(app) -> list[str]:
    rows = trace_rows(app)
    assert_trace_grid(rows, origin=ORIGIN)
    return _plain(rows)


@pytest.mark.asyncio
async def test_file_phrase_click_expands_only_its_file():
    """Фраза под диффом файла (решение 2026-10-26): клик раскрывает
    только этот файл, под ним — `− свернуть`; соседи свёрнуты."""
    app = _App()
    async with app.run_test(size=(80, 50)) as pilot:
        view = app.view()
        for key in "abc":
            _apply_edit(view, key, 8)
        await pilot.pause()
        lines = _shown_lines(app)
        assert sum(line == "     + ещё 5 строк" for line in lines) == 3
        await pilot.click(_phrase_of(app, "a:more"))
        await pilot.pause()
        lines = _shown_lines(app)
        assert sum("+line" in line for line in lines) == 8 + 3 + 3
        assert _phrase_of(app, "a:more").phrase == "− свернуть"
        assert _phrase_of(app, "b:more").phrase == "+ ещё 5 строк"
        a_end = lines.index("     − свернуть")
        assert "b.py" in lines[a_end + 2]


@pytest.mark.asyncio
async def test_space_on_header_toggles_block_on_file_row_its_diff():
    """Пробел на заголовке — весь блок, на строке файла — его дифф."""
    app = _App()
    async with app.run_test(size=(80, 60)) as pilot:
        view = app.view()
        rows = [_apply_edit(view, key, 8) for key in "abc"]
        await pilot.pause()
        header = next(w for w in app.query(ToolCallRow) if w.mode == "header")
        header.focus()
        await pilot.press("space")
        await pilot.pause()
        lines = _shown_lines(app)
        assert sum("+line" in line for line in lines) == 24
        assert sum(line == "     − свернуть" for line in lines) == 3
        await pilot.press("space")
        await pilot.pause()
        assert sum("+line" in line for line in _shown_lines(app)) == 9
        rows[1].focus()
        await pilot.press("space")
        await pilot.pause()
        assert sum("+line" in line for line in _shown_lines(app)) == 14
        assert _phrase_of(app, "b:more").phrase == "− свернуть"
        assert _phrase_of(app, "a:more").phrase == "+ ещё 5 строк"


@pytest.mark.asyncio
async def test_ran_output_and_files_phrases_under_owners():
    """`Ran` с выводом и файлами: фраза вывода — под выводом, над
    файлами; у каждого обрезанного файла — своя под его диффом. Пробел на
    строке файла — только его дифф, на строке команды — всё."""
    from gc_tui.models import CommandOutput
    from gc_tui.services.block_edits import RanResult

    app = _App()
    async with app.run_test(size=(80, 60)) as pilot:
        view = app.view()
        row = _row("x1", label="Run", target="make")
        view.add_call(row, C.RUN, waiting=False)
        view.started(row)
        out = tuple(f"out {i}" for i in range(6))
        output = CommandOutput(out, 0, 0, False, False, out[-3:], 3)
        files = tuple(
            _edit_of(_record(f"x1:{name}.py", 8)) for name in ("a", "b")
        )
        view.result(row, _ev("x1"), None, ran=RanResult(output, files=files))
        await pilot.pause()
        lines = [line for line in _shown_lines(app) if line]
        assert lines[1:5] == [
            "   └ … ещё 3 строки",
            "     out 3",
            "     out 4",
            "     out 5",
        ]
        assert lines[5].startswith("     Edited x1:a.py.py")
        phrases = [line for line in lines if "+ ещё" in line]
        assert phrases == ["     + ещё 5 строк"] * 2
        file_row = next(
            r for r in app.query(ToolCallRow) if r.call_id == "x1:b.py"
        )
        file_row.focus()
        await pilot.press("space")
        await pilot.pause()
        lines = _shown_lines(app)
        assert sum("+line" in line for line in lines) == 3 + 8
        assert "     out 0" not in lines
        row.focus()
        await pilot.press("space")
        await pilot.pause()
        lines = _shown_lines(app)
        assert sum("+line" in line for line in lines) == 16
        assert "   └ out 0" in lines


def _nested_lines(app) -> list[str]:
    """Строки вложенного уровня от его первого блока: сетка — от колонки
    его глифов (мини-лента ставит пустую строку под `Agent`)."""
    rows = trace_rows(app)
    start = next(i for i, r in enumerate(rows) if "Edited" in r.plain)
    assert_trace_grid(rows[start:], origin=ORIGIN + 2)
    return _plain(rows)


@pytest.mark.asyncio
async def test_nested_member_reason_under_member_text():
    """Уровень субагента — та же лестница на +2: `└` причины члена — под
    началом его текста."""
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view("sub1", depth=1)
        _read(view, "n0")
        _read(view, "n1", summary="не найден", outcome="error")
        await pilot.pause()
        rows = trace_rows(app)
        member = _line(rows, "n1.py")
        reason = _line(rows, "не найден")
        text = member.plain.index("Read")
        assert reason.plain.index("└") == text == ORIGIN + 2 + 4
        assert reason.plain.index("не найден") == text + 2


@pytest.mark.asyncio
async def test_nested_edits_phrase_under_each_file():
    """Правки субагента — то же правило на уровень глубже: фраза под
    диффом каждого обрезанного файла, клик раскрывает только его."""
    app = _App()
    async with app.run_test(size=(80, 50)) as pilot:
        top = app.view()
        agent = _row("a1", label="Agent", target="проверка")
        top.add_call(agent, C.AGENT, waiting=False)
        trace = app.query_one("#trace", VerticalScroll)
        model = ToolBlocks(app.timers, diff_preview=3, agent_window=True)
        nested = ToolBlocksView(_Host(trace), model, level="a1", depth=1)
        nested.set_owner(agent)
        app.views["a1"] = nested
        _apply_edit(nested, "a", 8)
        _apply_edit(nested, "b", 2)
        _apply_edit(nested, "c", 8)
        await pilot.pause()
        lines = _nested_lines(app)
        phrases = [i for i, line in enumerate(lines) if "+ ещё" in line]
        assert [lines[i] for i in phrases] == ["       + ещё 5 строк"] * 2
        b_row = next(i for i, line in enumerate(lines) if "b.py" in line)
        assert phrases[0] < b_row < phrases[1]
        await pilot.click(_phrase_of(app, "a:more"))
        await pilot.pause()
        lines = _nested_lines(app)
        assert sum("+line" in line for line in lines) == 8 + 2 + 3
        assert _phrase_of(app, "c:more").phrase == "+ ещё 5 строк"


@pytest.mark.asyncio
async def test_two_levels_route_toggle_to_own_model():
    app = _App()
    async with app.run_test(size=(80, 40)) as pilot:
        top = app.view()
        for i in range(4):
            _read(top, f"t{i}")
        top.apply(top.model.event_closing())
        nested = app.view("sub1", depth=1)
        nested_rows = [_read(nested, f"n{i}") for i in range(4)]
        await pilot.pause()
        nested_phrase = [
            p for p in app.query(BlockAction) if p.level == "sub1"
        ]
        await pilot.click(nested_phrase[0])
        await pilot.pause()
        assert all(r.display for r in nested_rows)
        hidden_top = [
            r
            for r in app.query(ToolCallRow)
            if r.call_id.startswith("t") and not r.display
        ]
        assert len(hidden_top) == 1


@pytest.mark.asyncio
async def test_issued_widgets():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        rows = [_read(view, f"c{i}") for i in range(4)]
        await pilot.pause()
        header = next(w for w in app.query(ToolCallRow) if w.mode == "header")
        phrase = app.query_one(BlockAction)
        assert view.issued(header)
        assert view.issued(phrase)
        assert all(view.issued(r) for r in rows)
        assert not view.issued(app.query_one("#trace"))


@pytest.mark.asyncio
async def test_hidden_focused_row_moves_focus_to_header():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        rows = [_read(view, f"c{i}") for i in range(3)]
        rows[0].focus()
        await pilot.pause()
        _read(view, "c3")
        await pilot.pause()
        header = next(w for w in app.query(ToolCallRow) if w.mode == "header")
        assert app.focused is header
        assert header.detail_call_id == "c3"


@pytest.mark.asyncio
async def test_hidden_by_agent_window_moves_focus_to_agent_row():
    """Окно `Agent` скрыло вложенный блок вместе с заголовком: фокус —
    на ближайший видимый уровень, строку `Agent` (§8.1)."""
    from textual.widgets import Static

    app = _App()
    async with app.run_test(size=(80, 30)) as pilot:
        top = app.view()
        _read(top, "c0")
        agent = _row("a1", label="Agent", target="проверка")
        top.add_call(agent, C.AGENT, waiting=False)
        trace = app.query_one("#trace", VerticalScroll)
        model = ToolBlocks(app.timers, agent_window=True)
        nested = ToolBlocksView(_Host(trace), model, level="a1", depth=1)
        nested.set_owner(agent)
        app.views["a1"] = nested
        row = _read(nested, "n0")
        await pilot.pause()
        row.focus()
        await pilot.pause()
        assert app.focused is row
        for i in range(3):
            prose = Static(f"проза {i}")
            await trace.mount(prose)
            nested.event_added(prose)
        await pilot.pause()
        assert app.focused is agent


@pytest.mark.asyncio
async def test_phrase_not_in_tab_chain_and_hover_accent():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        for i in range(4):
            _read(view, f"c{i}")
        await pilot.pause()
        phrase = app.query_one(BlockAction)
        assert phrase.can_focus is False
        assert phrase not in app.screen.focus_chain
        await pilot.hover(phrase)
        await pilot.pause()
        line = _line(trace_rows(app), "… ещё 1")
        col = line.plain.index("… ещё 1")
        assert _colour(line, col) == palette.ACCENT.lower()


@pytest.mark.asyncio
async def test_hidden_rows_not_selected():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        for i in range(5):
            _read(view, f"c{i}")
        await pilot.pause()
        trace = app.query_one("#trace")
        await pilot.mouse_down(trace, offset=(0, 0))
        await pilot.hover(trace, offset=(60, 10))
        await pilot.mouse_up(trace, offset=(60, 10))
        await pilot.pause()
        text = app.screen.get_selected_text() or ""
        assert "c4.py" in text
        assert "c0.py" not in text
        assert "c1.py" not in text


@pytest.mark.asyncio
async def test_set_result_without_model_keeps_old_row():
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        trace = app.query_one("#trace")
        row = _row("c0")
        await trace.mount(row)
        row.set_result(summary="12 строк", outcome="ok")
        await pilot.pause()
        line = _line(trace_rows(app), "a.py")
        assert line.plain.rstrip().endswith("a.py · 12 строк")


# --- следствие `Ran` -------------------------------------------------------


def _output(lines, **kw):
    from gc_tui.models import CommandOutput

    tail = tuple(lines[-3:])
    return CommandOutput(
        lines=tuple(lines),
        omitted_chars=0,
        exit_code=kw.get("exit_code", 0),
        timed_out=kw.get("timed_out", False),
        background=kw.get("background", False),
        tail=tail,
        hidden=len(lines) - len(tail),
    )


def test_output_texts_light_shows_retention_hint():
    """Вне окна retention раскрытие — хвост и подсказка, не весь вывод."""
    from gc_tui.services.footer_slot import RETENTION_DETAIL_HINT
    from gc_tui.services.block_edits import RanResult
    from gc_tui.widgets.tool_blocks_view import output_texts

    out = _output(["a", "b", "c", "d"])
    texts = output_texts(RanResult(output=out, light=True))
    assert texts.head is None
    assert texts.tail.plain == "b\nc\nd"
    assert texts.full.plain == f"b\nc\nd\n{RETENTION_DETAIL_HINT}"


def test_output_texts_head_lines():
    from gc_tui.services.block_edits import RanResult
    from gc_tui.widgets.tool_blocks_view import output_texts

    failed = output_texts(RanResult(output=_output(["e"], exit_code=2)))
    assert failed.head.plain == "код выхода 2"
    assert str(failed.head.style) == palette.ERR
    assert failed.tail.plain == failed.full.plain == "e"
    bare = output_texts(RanResult(output=_output([], exit_code=2)))
    assert bare.head.plain == "код выхода 2"
    assert bare.tail.plain == ""
    empty = output_texts(RanResult(output=_output([])))
    assert empty.head is None
    assert empty.tail.plain == "(нет вывода)"
    assert output_texts(RanResult(output=None)) is None


@pytest.mark.asyncio
async def test_ran_file_row_opens_command_detail():
    """Enter на строке файла под командой — экран деталей самой команды."""
    from gc_tui.services.block_edits import RanResult

    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        row = _row("x1", label="Run", target="make")
        view.add_call(row, C.RUN, waiting=False)
        view.started(row)
        edit = _edit_of(_record("x1:gen.py", 2))
        view.result(row, _ev("x1"), None, ran=RanResult(None, files=(edit,)))
        await pilot.pause()
        file_row = next(
            r for r in app.query(ToolCallRow) if r.call_id == "x1:gen.py"
        )
        assert file_row.detail_call_id == "x1"


@pytest.mark.asyncio
async def test_call_timer_is_core_time_and_turn_end_keeps_it():
    """Таймер вызова — по часам ядра: живой — время вызова, застывший на
    конце — то же время, что потом принесёт запись итога хода (`calls`).
    Часы самой строки видят вызов позже его старта: на 13 с сна они
    насчитали бы 12,6 с, и на конце хода строка прыгала бы `12с → 13с`."""
    from gc_core.persistence.turn_outcome import TurnOutcome

    from gc_tui.widgets.tool_blocks_view import end_turn

    core = {"w": 12.4}
    app = _App()
    async with app.run_test(size=(80, 24)) as pilot:
        view = app.view()
        row = ToolCallRow(call_id="w", label="Wait", target="13")
        row.set_clock_source(core.get)
        view.add_call(row, C.WAIT, waiting=False)
        view.started(row)
        row._start_monotonic -= 11.2
        assert row._render_content().plain.endswith("· 12с")
        row._start_monotonic -= 1.4
        core["w"] = 13.0
        view.result(row, _ev("w"), None)
        await pilot.pause()
        assert row._render_content().plain.endswith("· 13с")
        outcome = TurnOutcome(
            status="done",
            reason=None,
            opened_by=None,
            started_at="2026-10-26T10:00:00.000Z",
            ended_at="2026-10-26T10:00:28.000Z",
            duration_sec=28.0,
            error=None,
            calls={"w": 13.0},
        )
        end_turn(app.views, app.timers, outcome, settled=False)
        await pilot.pause()
        assert row._render_content().plain.endswith("· 13с")


# --- тогл — один кадр -------------------------------------------------------

_LONG = "packages/tui/src/gc_tui/widgets/very/long/directory/module_{}"


async def _one_frame(pilot, frames, level: str, key: str) -> list[str]:
    """Тогл ``key`` уровня ``level`` ложится одним кадром: после него —
    ровно один новый, без промежуточного."""
    await pilot.pause()
    del frames[:-1]
    pilot.app.post_message(BlockToggle(level, key))
    for _ in range(4):
        await pilot.pause()
    assert len(frames) == 2, frames[1:]
    return frames[-1]


def _long_reads(view, n: int) -> None:
    for i in range(n):
        key = f"r{i}"
        row = _row(key, target=_LONG.format(i) + ".py", depth=view.depth)
        view.add_call(row, C.READ, waiting=False)
        view.started(row)
        view.result(row, _ev(key, "12 строк"), None)


@pytest.mark.asyncio
async def test_explored_window_toggle_one_frame(monkeypatch):
    """Строка, скрытая окном с монтирования, ни разу не раскладывалась:
    объект сокращается по ширине ленты заранее — раскрытие окна не рисует
    несокращённый (перенесённый) объект кадром раньше."""
    frames = record_frames(monkeypatch)
    app = _App()
    async with app.run_test(size=(60, 30)) as pilot:
        _long_reads(app.view(), 4)
        opened = await _one_frame(pilot, frames, "", "block:0:top")
        assert_trace_grid(trace_rows(app), origin=ORIGIN)
        # Четыре члена — по строке: объект сокращён, хвост на месте.
        members = [line for line in opened if "Read  packages/" in line]
        assert len(members) == 4
        assert members[0].startswith("   └ Read  packages/")
        assert all(line.endswith("… · 12 строк") for line in members)
        await _one_frame(pilot, frames, "", "block:0:bottom")


@pytest.mark.asyncio
async def test_files_window_and_file_phrase_toggle_one_frame(monkeypatch):
    frames = record_frames(monkeypatch)
    app = _App()
    async with app.run_test(size=(60, 40)) as pilot:
        view = app.view()
        for i in range(5):
            _apply_edit(view, _LONG.format(i), 2)
        await _one_frame(pilot, frames, "", "block:0:top")
        assert_trace_grid(trace_rows(app), origin=ORIGIN)
        await _one_frame(pilot, frames, "", "block:0:bottom")
        _apply_edit(view, "long", 8)
        await _one_frame(pilot, frames, "", "long:more")
        await _one_frame(pilot, frames, "", "long:more")


@pytest.mark.asyncio
async def test_agent_window_toggle_one_frame(monkeypatch, steady_blink):
    frames = record_frames(monkeypatch)
    app = _App()
    async with app.run_test(size=(60, 40)) as pilot:
        top = app.view()
        agent = _row("a1", label="Agent", target="проверка")
        top.add_call(agent, C.AGENT, waiting=False)
        trace = app.query_one("#trace", VerticalScroll)
        model = ToolBlocks(app.timers, diff_preview=3, agent_window=True)
        nested = ToolBlocksView(_Host(trace), model, level="a1", depth=1)
        nested.set_owner(agent)
        app.views["a1"] = nested
        for i in range(4):
            row = _row(f"n{i}", label="Run", target=_LONG.format(i), depth=1)
            nested.add_call(row, C.RUN, waiting=False)
            nested.started(row)
            nested.result(row, _ev(f"n{i}"), None)
        await _one_frame(pilot, frames, "a1", "agent:top")
        await _one_frame(pilot, frames, "a1", "agent:bottom")


@pytest.mark.asyncio
async def test_ran_output_toggle_one_frame(monkeypatch):
    """Вывод `Ran` раскрывается на месте, без перемонтирования строк."""
    from gc_tui.services.block_edits import RanResult

    frames = record_frames(monkeypatch)
    app = _App()
    async with app.run_test(size=(60, 40)) as pilot:
        view = app.view()
        row = _row("x1", label="Run", target="make")
        view.add_call(row, C.RUN, waiting=False)
        view.started(row)
        out = tuple(f"out {i}" for i in range(9))
        output = CommandOutput(out, 0, 0, False, False, out[-3:], 6)
        view.result(row, _ev("x1"), None, ran=RanResult(output))
        opened = await _one_frame(pilot, frames, "", "block:0:output")
        assert "   └ out 0" in opened
        closed = await _one_frame(pilot, frames, "", "block:0:output")
        assert "   └ … ещё 6 строк" in closed
