"""Свежая строка хода — по ссылке экрана, без поиска по всей ленте.

Свежей бывает не больше одной строки — та, что последней получила
`freshen`; ссылка на снятую строку не возвращается (П4).
"""

from __future__ import annotations

import pytest
from test_turn_line_history import (
    TID,
    _answer,
    _app,
    _open,
    _prepare_one_turn_with_edits,
    _reply,
    _Thread,
    _turn_lines,
    _undo_setup,
    _write_thread,
)

TURNS = 24


def _prepare_turns(tmp_path, n=TURNS):
    _write_thread(tmp_path, tui={"status": "completed"})
    thread = _Thread(tmp_path)
    for i in range(1, n + 1):
        thread.messages(_reply(f"u{i}"), _answer(f"a{i}"))
        thread.record(i % 10, f"u{i}")


def _fresh_lines(scr):
    return [line for line in _turn_lines(scr) if line.is_fresh]


@pytest.mark.asyncio
async def test_open_many_turns_one_fresh_line(tmp_path):
    _prepare_turns(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        lines = _turn_lines(scr)
        assert len(lines) == TURNS
        assert _fresh_lines(scr) == [lines[-1]]
        assert scr._fresh_turn_line() is lines[-1]


@pytest.mark.asyncio
async def test_render_turns_does_not_query_turn_lines(tmp_path, monkeypatch):
    from gc_tui.widgets.turn_line import TurnLine

    _prepare_turns(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr = pilot.app.screen
        selectors = []
        query = scr.query

        def spy(selector=None):
            selectors.append(selector)
            return query(selector)

        monkeypatch.setattr(scr, "query", spy)
        scr.attach_active_task(TID, is_resume=True)
        await pilot.pause()

        assert len(_turn_lines(scr)) == TURNS
        assert TurnLine not in selectors
        assert "TurnLine" not in selectors


@pytest.mark.asyncio
async def test_fresh_ref_dropped_when_line_removed(tmp_path):
    _prepare_turns(tmp_path, 2)
    async with _app(tmp_path).run_test() as pilot:
        scr = await _open(pilot)
        line = scr._fresh_turn_line()
        assert line is _turn_lines(scr)[-1]

        line.remove()
        await pilot.pause()

        assert scr._fresh_turn_line() is None


@pytest.mark.asyncio
async def test_undo_restores_previous_fresh(tmp_path):
    _prepare_one_turn_with_edits(tmp_path)
    async with _app(tmp_path).run_test() as pilot:
        scr, _thread, _runner = await _undo_setup(pilot, tmp_path)
        (before,) = _turn_lines(scr)
        assert scr._fresh_turn_line() is None

        scr._cancel_with_undo()
        await pilot.pause()

        assert scr._fresh_turn_line() is before
        assert _fresh_lines(scr) == [before]
