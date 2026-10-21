"""Метка сжатия во всю ширину ленты (спека P3 §2.5, вариант B §10)."""

import re

import pytest
from gc_testkit.tui_support import tui_settings
from gc_tui.palette import DIM, FRAME_ARCHIVE
from gc_tui.services.trace_render import (
    build_compaction_marker_event,
    build_compaction_widgets,
    compaction_rule,
)
from rich.cells import cell_len

_COMP = {
    "ts": "t",
    "messages_in_segment": 34,
    "synthesis": "Цель: поднять таймаут клиента.",
    "chars_before": 128_300,
    "chars_after": 6_100,
}


def _label(text) -> str:
    """Подпись между полосами — без полос и пробелов по краям."""
    return text.plain.strip("─").strip()


def _styles(text) -> set:
    return {str(span.style) for span in text.spans}


@pytest.mark.parametrize(
    "width,label",
    [
        (120, "▸ контекст сжат #2 · 128k → 6k символов"),
        (60, "▸ контекст сжат #2 · 128k → 6k символов"),
        (40, "▸ контекст сжат #2 · 128k → 6k"),
        (28, "▸ сжат #2 · 128k → 6k"),
        (18, "▸ сжат #2"),
    ],
)
def test_rule_fills_width_with_ladder_step(width, label):
    text = compaction_rule(2, _COMP, width, expanded=False)
    assert "\n" not in text.plain
    assert cell_len(text.plain) == width
    assert _label(text) == label
    assert re.fullmatch(r"─{2,} .+ ─{2,}", text.plain)


def test_rule_mockup_widths():
    """Ширины ленты макетов p3_compaction_60 / _40: полосы 8/9 и 3/3."""
    wide = compaction_rule(2, _COMP, 58, expanded=False).plain
    assert wide.startswith("─" * 8 + " ▸") and wide.endswith(
        "символов " + "─" * 9
    )
    narrow = compaction_rule(2, _COMP, 38, expanded=False).plain
    assert narrow == "───" + " ▸ контекст сжат #2 · 128k → 6k " + "───"


def test_rule_colors_bars_archive_label_dim():
    text = compaction_rule(2, _COMP, 80, expanded=False)
    assert _styles(text) == {FRAME_ARCHIVE, DIM}
    first = text.spans[0]
    assert str(first.style) == FRAME_ARCHIVE
    assert set(text.plain[first.start : first.end]) == {"─"}


def test_rule_expanded_arrow_down():
    text = compaction_rule(2, _COMP, 80, expanded=True)
    assert _label(text).startswith("▾ контекст сжат #2")


def test_rule_too_narrow_truncates_label_with_ellipsis():
    text = compaction_rule(2, _COMP, 10, expanded=False)
    assert cell_len(text.plain) == 10
    assert re.fullmatch(r"─{2} .+ ─{2}", text.plain)
    assert "…" in text.plain


def test_rule_without_numbers_keeps_full_words():
    """Сводка без счёта (запись dialog_summary вне сегментов) — без чисел."""
    text = compaction_rule(None, {}, 60, expanded=False)
    assert _label(text) == "▸ контекст сжат"


def test_marker_event_carries_compaction():
    ev = build_compaction_marker_event(5, 1, _COMP)
    assert ev.kind == "summary"
    assert ev.full_payload == {"num": 2, "comp": _COMP}
    assert ev.summary == "контекст сжат #2 · 128k → 6k символов"
    assert ev.summary_content == _COMP["synthesis"]


@pytest.mark.asyncio
async def test_widgets_in_trace_fill_width_and_resize():
    pytest.importorskip("textual")
    from textual.app import App
    from textual.containers import VerticalScroll

    ev = build_compaction_marker_event(4, 1, _COMP)

    class _A(App):
        def compose(self):
            yield VerticalScroll(id="t")

        def on_mount(self):
            rule, body = build_compaction_widgets(ev, focusable=False)
            self.query_one("#t").mount(rule, body)

    async with _A().run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        rule = pilot.app.query_one(".summary-marker")
        plain = rule._content.plain
        assert cell_len(plain) == rule.size.width
        assert "символов" in plain
        await pilot.resize_terminal(40, 20)
        await pilot.pause()
        plain = rule._content.plain
        assert cell_len(plain) == rule.size.width
        assert "символов" not in plain and "128k → 6k" in plain


@pytest.mark.asyncio
async def test_body_lines_and_collapse_click(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.messages import SummaryToggleRequest

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._render_event(build_compaction_marker_event(3, 1, _COMP))
        await pilot.pause()
        rule = scr.query_one(".summary-marker")
        body = scr.query_one(".summary-body")
        assert body.display is False
        scr.on_summary_toggle_request(SummaryToggleRequest(3))
        await pilot.pause()
        assert body.display is True
        assert "▾ контекст сжат #2" in rule._content.plain
        head = body.query_one(".compaction-head")
        assert str(head.render()) == (
            "Контекст сохранён: 34 сообщения → сводка"
        )
        assert "Цель: поднять" in str(
            body.query_one(".compaction-text").render()
        )
        action = body.query_one(".trace-action")
        assert str(action.render()) == "− свернуть"
        # Строку подводим в зону видимости и проверяем попадание курсора —
        # иначе тест «прошёл бы» на промахе.
        action.scroll_visible(animate=False)
        await pilot.pause()
        await pilot.pause()
        hit, _ = pilot.app.get_widget_at(*action.region.offset)
        assert hit is action
        await pilot.click(action)
        await pilot.pause()
        assert body.display is False
        assert "▸ контекст сжат #2" in rule._content.plain


def _widget_shape(rule, body) -> tuple:
    return (
        type(rule).__name__,
        sorted(rule.classes),
        rule.can_focus,
        type(body).__name__,
        sorted(body.classes),
        body._head,
        body._text,
    )


def test_live_and_preview_share_builder():
    """Живой путь и превью строят одни и те же виджеты одним билдером —
    разница только в фокусе (Enter превью открывает тред)."""
    ev = build_compaction_marker_event(4, 1, _COMP)
    live = build_compaction_widgets(ev, focusable=True)
    preview = build_compaction_widgets(ev, focusable=False)
    assert _widget_shape(*live)[3:] == _widget_shape(*preview)[3:]
    assert _widget_shape(*live)[:2] == _widget_shape(*preview)[:2]
    assert live[0].can_focus is True and preview[0].can_focus is False


def test_no_body_without_synthesis():
    ev = build_compaction_marker_event(4, 0, {**_COMP, "synthesis": ""})
    assert build_compaction_widgets(ev, focusable=True)[1] is None
