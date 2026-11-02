"""Строка мысли модели в ленте (спека мыслей §2): `● Thinking|Thought
 <текст> · <таймер>`, всё `DIM`; у идущей — хвост с `…` в начале, у
законченной — последнее предложение с начала, `…` в начале, если до
него был текст, и в конце при обрезке;
таймер не режется; клик по обрезанному тексту и пробел раскрывают мысль
целиком под строкой (сама строка — без текста), `− свернуть` внизу
сворачивает."""

from __future__ import annotations

import pytest
from gc_tui.messages import OpenEventRequest
from gc_tui.palette import DIM
from gc_tui.services.trace_render import THOUGHT_GLYPH, thought_line
from rich.cells import cell_len

_LONG = "слово " * 40 + "итог"


def build_line(**kwargs) -> str:
    """Строка мысли из частей `thought_line` — как её собирает виджет."""
    parts = thought_line(**kwargs)
    return THOUGHT_GLYPH + parts.lead + parts.tail + parts.timer


def test_thought_line_live_and_done():
    live = build_line(live=True, text="a b c", seconds=4, width=60)
    done = build_line(live=False, text="a b c", seconds=6, width=60)
    assert live == "● Thinking  a b c · 4с"
    assert done == "● Thought  a b c · 6с"


def test_thought_line_parts_mark_cut():
    fits = thought_line(live=False, text="a b", seconds=3, width=40)
    cut = thought_line(live=False, text=_LONG, seconds=3, width=40)
    assert (fits.word, fits.tail, fits.timer, fits.cut) == (
        "Thought",
        "a b",
        " · 3с",
        False,
    )
    assert cut.cut and cut.tail.endswith("…")


def test_thinking_line_cuts_head_keeps_timer():
    line = build_line(live=True, text="слово " * 40, seconds=7, width=40)
    assert line.startswith("● Thinking  …")
    assert line.endswith(" · 7с")
    assert cell_len(line) <= 40


def test_thinking_single_long_word_fits_width():
    line = build_line(live=True, text="a" * 100, seconds=7, width=30)
    assert line.startswith("● Thinking  …a")
    assert cell_len(line) == 30


_TWO = (
    "The user is asking what I can do. Let me provide a helpful overview "
    "of my capabilities without making it too long or comprehensive."
)


def test_thought_shows_last_sentence_from_start():
    line = build_line(live=False, text=_TWO, seconds=3, width=60)
    assert line == (
        "● Thought  …Let me provide a helpful overview of my… · 3с"
    )


def test_thought_last_sentence_fits_whole():
    parts = thought_line(
        live=False, text="Сначала план. Читаю файл.", seconds=None, width=30
    )
    assert (parts.tail, parts.cut) == ("…Читаю файл.", True)


def test_thought_without_sentences_cuts_end():
    line = build_line(live=False, text="слово " * 40, seconds=7, width=40)
    assert line.startswith("● Thought  слово слово")
    assert line.endswith("… · 7с")
    assert cell_len(line) <= 40


def test_thought_single_long_word_fits_width():
    line = build_line(live=False, text="a" * 100, seconds=7, width=30)
    assert line.startswith("● Thought  a")
    assert line.endswith("… · 7с")
    assert cell_len(line) == 30


def test_expanded_line_has_no_text():
    for live, word in ((False, "Thought"), (True, "Thinking")):
        parts = thought_line(
            live=live, text=_TWO, seconds=3, width=60, expanded=True
        )
        assert THOUGHT_GLYPH + parts.lead + parts.tail + parts.timer == (
            f"● {word} · 3с"
        )
        assert parts.cut


def test_thought_line_without_duration():
    line = build_line(live=False, text="x", seconds=None, width=40)
    assert line == "● Thought  x"


def test_thought_line_no_zero_timer():
    line = build_line(live=True, text="x", seconds=0.4, width=40)
    assert line == "● Thinking  x"


def test_thought_line_collapses_whitespace():
    line = build_line(live=False, text="план\n  читаю", seconds=None, width=40)
    assert line == "● Thought  план читаю"


def test_reasoning_tail_is_public():
    from gc_tui.services.stream_sink import reasoning_tail

    assert reasoning_tail("alpha beta gamma delta", 12) == "…gamma delta"


def _app(row, opened: list[int] | None = None):
    from textual.app import App
    from textual.containers import VerticalScroll

    class _A(App):
        def compose(self):
            yield VerticalScroll(row, id="t")

        def on_open_event_request(self, message: OpenEventRequest) -> None:
            if opened is not None:
                opened.append(message.event_no)

    return _A()


def _text_static(row):
    return row.query_one(".thought-text")


def _line(row) -> str:
    from textual.widgets import Static

    statics = row.query_one(".thought-line").query(Static)
    return "".join(str(s.render()) for s in statics)


@pytest.mark.asyncio
async def test_row_fits_width_and_marks_expandable():
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=_LONG, live=False, attempt=None, seconds=7)
    async with _app(row).run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        line = _line(row)
        assert line.startswith("● Thought  слово")
        assert line.endswith("… · 7с")
        assert cell_len(line) < 50
        assert _text_static(row).has_class("trace-expandable")


@pytest.mark.asyncio
async def test_short_text_is_not_expandable():
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text="коротко", live=False, attempt=None, seconds=2)
    async with _app(row).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        assert _line(row) == "● Thought  коротко · 2с"
        assert not _text_static(row).has_class("trace-expandable")
        await pilot.click(_text_static(row))
        await pilot.pause()
        assert not row.expanded


@pytest.mark.asyncio
async def test_click_on_text_expands_without_selecting_the_row():
    """Клик по обрезанному тексту раскрывает мысль, но строку не выделяет:
    обычный клик в ленте не выделяет (канон клик-модели)."""
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=_LONG, live=False, attempt=None, seconds=7)
    async with _app(row).run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        await pilot.click(_text_static(row))
        await pilot.pause()
        assert row.expanded
        assert not row.has_focus


@pytest.mark.asyncio
async def test_click_on_text_expands_and_collapse_at_bottom():
    from gc_tui.widgets.expandable_text import ExpandableTextBody
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=_LONG, live=False, attempt=None, seconds=7)
    async with _app(row).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        body = row.query_one(ExpandableTextBody)
        assert body.display is False
        await pilot.click(_text_static(row))
        await pilot.pause()
        assert row.expanded and body.display is True
        assert _line(row) == "● Thought · 7с"
        assert str(body.query_one(".expandable-text").render()) == (
            " ".join(_LONG.split())
        )
        collapse = body.query_one(".trace-action")
        assert str(collapse.render()) == "− свернуть"
        await pilot.click(collapse)
        await pilot.pause()
        assert not row.expanded and body.display is False
        assert _line(row).startswith("● Thought  слово")


@pytest.mark.asyncio
async def test_space_toggles_and_enter_opens_nothing():
    """Пробел раскрывает и сворачивает; экрана события у мысли нет —
    Enter ничего не открывает."""
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=_LONG, live=False, attempt=None, seconds=7)
    opened: list[int] = []
    async with _app(row, opened).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        row.focus()
        await pilot.press("space")
        await pilot.pause()
        assert row.expanded
        await pilot.press("space")
        await pilot.pause()
        assert not row.expanded
        await pilot.press("enter")
        await pilot.pause()
    assert opened == []


@pytest.mark.asyncio
async def test_live_update_grows_open_body():
    from gc_tui.widgets.expandable_text import ExpandableTextBody
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=_LONG, live=True, attempt=None, seconds=1)
    async with _app(row).run_test(size=(50, 30)) as pilot:
        await pilot.pause()
        assert _line(row).startswith("● Thinking  …")
        row.toggle()
        row.update_live(_LONG + " дальше", 3, True)
        await pilot.pause()
        body = row.query_one(ExpandableTextBody)
        rendered = str(body.query_one(".expandable-text").render())
        assert rendered.endswith("итог дальше")
        assert _line(row) == "● Thinking · 3с"


@pytest.mark.asyncio
async def test_settle_stops_blink_and_freezes_text():
    """Попытка кончилась: строка законченная, мигание стоп, стрим её
    больше не трогает."""
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text="мысль", live=True, attempt="a2", seconds=4.2)
    async with _app(row).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        assert row._pulse_timer is not None
        assert not row.settled
        row.settle()
        row.update_live("чужой текст", 9, True)
        await pilot.pause()
        assert row.settled and row.attempt == "a2"
        assert row._pulse_timer is None
        assert _line(row) == "● Thought  мысль · 4с"


def test_glyph_blinks_dim_with_running_rhythm():
    """Ритм мигания — тот же, что у идущего вызова; цвет — `DIM`."""
    from gc_tui.services.trace_glyphs import blink_on
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text="x", live=True, attempt=None, seconds=None)
    seen = []
    for frame in range(24):
        row._pulse_frame = frame
        glyph = row._render_glyph()
        seen.append(glyph.plain[0])
        assert DIM in str(glyph.spans[0].style)
    assert seen == ["●" if blink_on(f) else " " for f in range(24)]
    row.update_live("x", 2, False)
    row._pulse_frame = 8
    assert row._render_glyph().plain[0] == "●"


def _inline_styles(static) -> set[str]:
    content = static.content
    return {str(span.style) for span in getattr(content, "spans", [])}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "text,cut", [("a b", False), (_LONG, True)], ids=["fits", "cut"]
)
async def test_thought_colors_are_dim(text, cut):
    """Глиф, слово и таймер — инлайн `DIM`; хвост без инлайн-стиля (цвет
    и ховер — CSS), класс `trace-expandable` — только у обрезанного."""
    from gc_tui.widgets.thought_row import ThoughtRow

    row = ThoughtRow(text=text, live=False, attempt=None, seconds=3)
    async with _app(row).run_test(size=(50, 20)) as pilot:
        await pilot.pause()
        for selector in (".trace-prefix", ".thought-word", ".thought-timer"):
            static = row.query_one(selector)
            assert str(static.render()).strip(), selector
            assert _inline_styles(static) == {DIM}, selector
        tail = _text_static(row)
        assert isinstance(tail.content, str)
        assert tail.has_class("trace-expandable") is cut
