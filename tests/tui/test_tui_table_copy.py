"""Копирование markdown-таблиц и code-блоков из трейса (спека 2026-08-02).

Покрытие §7 тест-плана:
1. table_block_to_markdown — базовая, aligns, round-trip, escape, пустая.
2. get_selection — с copy_text → (md, "\\n") при любом Selection; без → fallback.
3. selection_updated — класс -copy-selected on/off; без copy_text не ставится.
4. render_code_block / build_block_widgets — copy_text передаётся в виджет.
"""
import pytest

pytest.importorskip("textual")

from tui.services.markdown_render import (
    segment_answer,
    table_block_to_markdown,
)


# ---- table_block_to_markdown ----

def test_table_block_to_markdown_basic():
    """3 колонки / 2 строки → валидный markdown с header + separator + rows."""
    md = table_block_to_markdown(
        ["A", "B", "C"], [["1", "2", "3"], ["4", "5", "6"]],
        ["left", "left", "left"],
    )
    lines = md.split("\n")
    assert lines[0] == "| A | B | C |"
    assert lines[1] == "| --- | --- | --- |"
    assert lines[2] == "| 1 | 2 | 3 |"
    assert lines[3] == "| 4 | 5 | 6 |"
    assert len(lines) == 4


def test_table_block_to_markdown_aligns():
    """left → ``---``, center → ``:---:``, right → ``---:``."""
    md = table_block_to_markdown(
        ["L", "C", "R"], [["a", "b", "c"]],
        ["left", "center", "right"],
    )
    sep = md.split("\n")[1]
    assert sep == "| --- | :---: | ---: |"


def test_table_block_to_markdown_round_trip():
    """Round-trip: segment_answer(table_block_to_markdown(...)) == исходный Block
    на «чистых» ячейках (без ``|``)."""
    headers = ["Name", "Score"]
    rows = [["Alice", "99"], ["Bob", "42"]]
    aligns = ["left", "right"]
    md = table_block_to_markdown(headers, rows, aligns)
    segs = segment_answer(md)
    assert len(segs) == 1 and segs[0].kind == "table"
    assert segs[0].headers == headers
    assert segs[0].rows == rows
    # aligns: round-trip теряет left/right-различие (``---`` vs ``---:``),
    # но left остаётся left, right — right.
    assert segs[0].aligns == ["left", "right"]


def test_table_block_to_markdown_escape_pipe():
    """Ячейка ``a|b`` → в выводе ``a\\|b`` (без round-trip через парсер)."""
    md = table_block_to_markdown(["H"], [["a|b"]], ["left"])
    assert r"a\|b" in md
    # строка ячейки содержит экранированную pipe, не разрывает колонку
    cell_line = md.split("\n")[2]
    assert cell_line == "| a\\|b |"


def test_table_block_to_markdown_empty():
    """Пустые headers/rows → только header + separator, без строк данных.
    Крайний случай: ``" | ".join([])`` = "" → строка ``"|  |"``."""
    md = table_block_to_markdown([], [], [])
    lines = md.split("\n")
    assert lines[0] == "|  |"  # "| " + "" + " |"
    assert lines[1] == "|  |"
    assert len(lines) == 2


# ---- get_selection ----

def test_get_selection_with_copy_text_returns_markdown():
    """С copy_text → (md, "\\n") при любом Selection, игнорируя координаты."""
    from rich.text import Text
    from tui.widgets.agent_reply_line import AgentReplyLine

    w = AgentReplyLine(Text("table"), event_no=1, copy_text="| A |\n| --- |")
    # selection не используется override'ом — передаём dummy
    result = w.get_selection(selection=object())
    assert result == ("| A |\n| --- |", "\n")


@pytest.mark.asyncio
async def test_get_selection_without_copy_text_delegates_to_super():
    """Без copy_text → _copy_text is None → fallback на super().get_selection,
    который для rich.Text отдаёт selection.extract(text) + "\n".

    Проверяем реальную делегацию: строим валидный Selection по координатам
    текста "hello\nworld" и убеждаемся, что override не перехватил вызов —
    super() извлёк подстроку по координатам, а не вернул весь текст целиком.
    (super() зовёт self._render() → нужен активный App, поэтому через run_test.)
    """
    from textual.app import App, ComposeResult
    from textual.geometry import Offset
    from textual.selection import Selection
    from rich.text import Text
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine(Text("hello\nworld"), event_no=1)

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        assert w._copy_text is None

        # Offset(x, y): x — колонка, y — строка. (0,0)→(3,1) накрывает
        # строки 0..1, на второй строке до 3-го символа → "hello\nwor".
        sel = Selection.from_offsets(Offset(0, 0), Offset(3, 1))
        result = w.get_selection(sel)
        # super() для Text возвращает (extracted, "\n") — не None и не весь текст.
        assert result is not None
        extracted, ending = result
        assert ending == "\n"
        assert "hello" in extracted
        assert "wor" in extracted
        # Убеждается, что это именно координатный срез, а не весь контент целиком:
        # "ld" (хвост второй строки вне selection) не должен попасть в буфер.
        assert "world" not in extracted


# ---- selection_updated ----

@pytest.mark.asyncio
async def test_selection_updated_sets_copy_selected_class():
    """selection is not None → класс ``-copy-selected`` на виджете."""
    from textual.app import App, ComposeResult
    from rich.text import Text
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine(Text("t"), event_no=1, copy_text="md")

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        assert not w.has_class("-copy-selected")
        w.selection_updated(selection=object())  # non-None
        await pilot.pause()
        assert w.has_class("-copy-selected")


@pytest.mark.asyncio
async def test_selection_updated_clears_copy_selected_class():
    """selection=None → класс ``-copy-selected`` снят."""
    from textual.app import App, ComposeResult
    from rich.text import Text
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine(Text("t"), event_no=1, copy_text="md")

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        w.selection_updated(selection=object())  # set
        await pilot.pause()
        assert w.has_class("-copy-selected")
        w.selection_updated(selection=None)  # clear
        await pilot.pause()
        assert not w.has_class("-copy-selected")


@pytest.mark.asyncio
async def test_selection_updated_no_class_without_copy_text():
    """Без copy_text класс ``-copy-selected`` не ставится (Text-ветка — native)."""
    from textual.app import App, ComposeResult
    from rich.text import Text
    from tui.widgets.agent_reply_line import AgentReplyLine

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine(Text("prose"), event_no=1)

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        w.selection_updated(selection=object())
        await pilot.pause()
        assert not w.has_class("-copy-selected")


@pytest.mark.asyncio
async def test_selection_updated_swaps_syntax_background():
    """Syntax-ветка: selection → background_color=FOCUS_BG; clear → BAND_BG.

    CSS background не перекрывает ячейки rich.Syntax — подсветка идёт
    in-place через background_color (спека 2026-08-02).
    """
    from textual.app import App, ComposeResult
    from rich.syntax import Syntax
    from tui.palette import BAND_BG, FOCUS_BG
    from tui.widgets.agent_reply_line import AgentReplyLine

    syntax = Syntax(
        "print(1)", lexer="python", theme="monokai",
        background_color=BAND_BG, word_wrap=False,
    )

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AgentReplyLine(syntax, event_no=1, copy_text="```python\nprint(1)\n```")

    async with _App().run_test() as pilot:
        w = pilot.app.query_one(AgentReplyLine)
        assert isinstance(w._select_renderable, Syntax)
        assert w._select_renderable.background_color == BAND_BG

        w.selection_updated(selection=object())
        await pilot.pause()
        assert w.has_class("-copy-selected")
        assert w._select_renderable.background_color == FOCUS_BG

        w.selection_updated(selection=None)
        await pilot.pause()
        assert not w.has_class("-copy-selected")
        assert w._select_renderable.background_color == BAND_BG


# ---- render_code_block / build_block_widgets: copy_text передаётся ----

def test_render_code_block_with_lang_has_copy_text():
    """render_code_block с известным lang → fenced copy_text на виджете."""
    from tui.services.trace_render import render_code_block

    w = render_code_block("print(1)", "python", event_no=1)
    assert w._copy_text is not None
    assert w._copy_text == "```python\nprint(1)\n```"


def test_render_code_block_unknown_lang_no_copy_text():
    """render_code_block с неизвестным lang → fallback на Text, copy_text=None
    (нативный partial select, не RichVisual)."""
    from tui.services.trace_render import render_code_block

    w = render_code_block("x", "zzz_bogus", event_no=1)
    assert w._copy_text is None


@pytest.mark.asyncio
async def test_build_block_widgets_table_has_copy_text():
    """Таблица в build_block_widgets → AgentReplyLine с copy_text=markdown."""
    from tui.services.trace_render import build_block_widgets
    from tui.widgets.agent_reply_line import AgentReplyLine

    text = "| A | B |\n|---|---|\n| 1 | 2 |"
    widgets = build_block_widgets(text, prose_bullet=True, event_no=1)
    table_w = [w for w in widgets if isinstance(w, AgentReplyLine)]
    assert len(table_w) == 1
    assert table_w[0]._copy_text is not None
    assert "| A | B |" in table_w[0]._copy_text
    assert "| 1 | 2 |" in table_w[0]._copy_text
