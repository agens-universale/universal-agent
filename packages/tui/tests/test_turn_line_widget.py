"""Строка итога хода: свежая, осевшая и «за баром» (S10 §3.2, §3.4).

Свежая строка (`freshen`) — голова, справа сводка правок, под «✕ Ошибка» —
причина `└` (до трёх строк). Осевшая (`settle`) — только голова, но данные
сводки и причины строка хранит. `hold` прячет строку, пока стоит бар.
"""

from __future__ import annotations

import pytest
from gc_core.persistence.turn_outcome import TurnOutcome
from gc_testkit.paths import module_dir
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
async def test_fresh_error_reason_wraps_to_three_rows_settled_one():
    line = _failed_line()
    async with LineApp(line).run_test(size=(60, 20)) as pilot:
        line.settle()
        line.freshen(None, "x" * 200)
        await pilot.pause()
        rows = line.render().plain.split("\n")
        assert rows[0] == "✕ Ошибка"
        assert 1 < len(rows) <= 4
        assert rows[1].lstrip().startswith("└")

        line.settle()
        await pilot.pause()
        assert line.region.height == 1
        assert line.error == "x" * 200


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
