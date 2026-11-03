"""Строка итога хода: свежая, осевшая и «за баром» (S10 §3.2, §3.4).

Свежая строка (`freshen`) — голова, справа сводка правок. Осевшая
(`settle`) — без сводки, но данные сводки и причины строка хранит. `hold`
прячет строку, пока стоит бар.

Причина ошибки (спека предохранителя §2.4, ревизия 3) — как у строки мысли:
свёрнутая — тусклый хвост ` · <причина>` в одну строку с `…`; клик по
обрезанному хвосту или пробел разворачивают полный текст под строкой с
`− свернуть`; влезающая причина не разворачивается; свежая строка ошибки
развёрнута, осевшая — свёрнута. Подсказки нет.
"""

from __future__ import annotations

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.paths import module_dir
from gc_testkit.tui_support import span_style
from gc_tui.services.trace_render import build_turn_line
from textual.app import App, ComposeResult


class LineApp(App):
    """Одна строка итога на экране со стилями и палитрой TUI."""

    CSS_PATH = str(module_dir("tui") / "styles.tcss")

    def __init__(self, line) -> None:
        super().__init__()
        self.line = line

    def get_css_variables(self) -> dict:
        from gc_tui.palette import css_variables

        variables = super().get_css_variables()
        variables.update(css_variables())
        return variables

    def compose(self) -> ComposeResult:
        yield self.line


def _failed_line():
    return build_turn_line(
        TurnOutcome(
            status="error",
            reason=None,
            opened_by="u1",
            started_at="2026-10-22T11:30:00Z",
            ended_at="",
            duration_sec=3.0,
            error="boom",
        )
    )


def test_line_keeps_word_and_record_flag():
    line = _failed_line()
    assert line.word == "failed"
    assert line.recorded is True
    assert build_turn_line(word="interrupted").recorded is False


@pytest.mark.asyncio
async def test_fresh_line_shows_summary_and_settled_keeps_data():
    line = build_turn_line(word="completed")
    async with LineApp(line).run_test(size=(80, 10)) as pilot:
        line.freshen((2, 14, 3), None)
        await pilot.pause()
        assert line.is_fresh
        assert line.render().plain.endswith("изменено 2 файла · +14 −3")
        assert "изменено" in line.render().plain

        line.settle()
        await pilot.pause()
        assert not line.is_fresh
        assert line.render().plain == "✓ Выполнил"
        assert "изменено" not in line.render().plain
        assert line.summary == (2, 14, 3)


@pytest.mark.asyncio
async def test_hold_hides_line_and_reveal_shows_it():
    line = build_turn_line(word="completed")
    async with LineApp(line).run_test(size=(80, 10)) as pilot:
        line.hold()
        await pilot.pause()
        assert line.display is False

        line.reveal()
        await pilot.pause()
        assert line.display is True


@pytest.mark.asyncio
async def test_narrow_line_hides_the_whole_segment():
    line = build_turn_line(word="completed")
    async with LineApp(line).run_test(size=(24, 10)) as pilot:
        line.freshen((2, 14, 3), None)
        await pilot.pause()
        assert line.render().plain == "✓ Выполнил"
        assert "изменено" not in line.render().plain


_LONG = "MainModelUnavailable: " + "модель недоступна " * 8  # ~150 симв.


def _plain(line) -> str:
    return line.render().plain


def _body(line):
    """Тело развёрнутой причины — соседний виджет за строкой."""
    (body,) = line.screen.query(".turn-reason")
    return body


def _body_text(line) -> str:
    return str(_body(line).query_one(".expandable-text").render())


async def _click_tail(pilot, line):
    start, _end = line._tail_span
    await pilot.click(line, offset=(start + 1, 0))
    await pilot.pause()


async def _settled(pilot, line, reason=_LONG):
    line.freshen(None, reason)
    line.settle()
    await pilot.pause()


@pytest.mark.asyncio
async def test_settled_error_shows_dim_reason_tail():
    from gc_tui.palette import DIM

    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 10)) as pilot:
        await _settled(pilot, line)
        text = _plain(line)
        assert "\n" not in text
        assert text.startswith("✕ Ошибка · MainModelUnavailable: модель")
        assert text.endswith("…")
        assert line.expanded is False
        assert line.region.height == 1
        assert span_style(line.render(), "MainModel") == DIM


@pytest.mark.asyncio
async def test_hover_on_cut_tail_lights_it_ink():
    from gc_tui.palette import INK

    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 10)) as pilot:
        await _settled(pilot, line)
        start, _end = line._tail_span
        await pilot.hover(line, offset=(start + 1, 0))
        await pilot.pause()
        assert span_style(line.render(), "MainModel") == INK
        await pilot.hover(line, offset=(0, 0))
        await pilot.pause()
        assert span_style(line.render(), "MainModel") != INK


@pytest.mark.asyncio
async def test_click_tail_expands_full_reason_with_collapse():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        await _settled(pilot, line)
        await _click_tail(pilot, line)
        assert line.expanded is True
        assert _plain(line) == "✕ Ошибка"
        body = _body(line)
        assert body.display is True
        assert " ".join(_body_text(line).split()) == " ".join(_LONG.split())
        collapse = body.query_one(".expandable-collapse")
        assert str(collapse.render()) == "− свернуть"
        assert body.region.y == line.region.bottom

        await pilot.click(collapse)
        await pilot.pause()
        assert line.expanded is False
        assert body.display is False


@pytest.mark.asyncio
async def test_space_toggles_selected_error_line():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        await _settled(pilot, line)
        line.focus()
        await pilot.press("space")
        assert line.expanded is True
        await pilot.press("space")
        assert line.expanded is False


@pytest.mark.asyncio
async def test_fitting_reason_is_not_expandable():
    line = _failed_line()
    async with LineApp(line).run_test(size=(80, 10)) as pilot:
        await _settled(pilot, line, "ReadError: boom")
        assert _plain(line) == "✕ Ошибка · ReadError: boom"
        assert line._tail_span is None
        await pilot.click(line, offset=(14, 0))
        await pilot.pause()
        assert line.expanded is False
        line.focus()
        await pilot.press("space")
        assert line.expanded is False
        assert list(line.screen.query(".turn-reason")) == []


@pytest.mark.asyncio
async def test_fresh_error_line_expanded_by_default_and_settle_collapses():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        line.freshen(None, _LONG)
        await pilot.pause()
        assert line.expanded is True
        assert _plain(line) == "✕ Ошибка"
        assert _body(line).display is True

        line.settle()
        await pilot.pause()
        assert line.expanded is False
        assert _body(line).display is False
        assert _plain(line).startswith("✕ Ошибка · MainModel")
        assert line.error == _LONG


@pytest.mark.asyncio
async def test_held_line_hides_and_reveals_body_with_it():
    line = _failed_line()
    line.freshen(None, _LONG)
    line.hold()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        await pilot.pause()
        body = _body(line)  # смонтировано за баром — встанет вместе
        assert body.display is False
        line.reveal()
        assert body.display is True


@pytest.mark.asyncio
async def test_removed_line_takes_body_along():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        line.freshen(None, _LONG)
        await pilot.pause()
        screen = line.screen
        assert list(screen.query(".turn-reason"))
        await line.remove()
        await pilot.pause()
        assert list(screen.query(".turn-reason")) == []


@pytest.mark.asyncio
async def test_error_reason_keeps_square_brackets():
    reason = "ошибка [bold]x[/] a [/] b " + "длинный хвост " * 8
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        await _settled(pilot, line, reason)
        assert "ошибка [bold]x[/] a [/] b" in _plain(line)
        line.toggle()
        await pilot.pause()
        assert _body_text(line).startswith("ошибка [bold]x[/] a [/] b")


@pytest.mark.asyncio
async def test_no_tooltip_on_error_line():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 10)) as pilot:
        line.freshen(None, _LONG)
        await pilot.pause()
        assert line.tooltip is None
        line.settle()
        await pilot.pause()
        assert line.tooltip is None
