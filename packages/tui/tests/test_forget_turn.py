"""«Забыть ход» (спека шага 2 окна ленты, §5.3, §8 «Забыть ход»): снятый
ход не держит ни один реестр экрана; ход с живыми строками, текущий и
не верхний ход не снимаются."""

from __future__ import annotations

import pytest
from feed_dump import HISTORY_THREAD, load_history_fixture, write_history
from gc_tui.screens.main import _bound_widget
from test_trace_ephemera import _SIZE, _app, live_session


def _held(scr) -> list:
    """Все виджеты, на которые держат ссылку реестры экрана (§5.3 и
    добавленные окном ленты)."""
    held = [
        *scr._active_calls.values(),
        *scr._subagent_parent_rows.values(),
        *scr._nested_tool_rows.values(),
        *(w for ws in scr._nested_widgets_by_parent.values() for w in ws),
        *scr._summary_marker_widgets.values(),
        *scr._summary_body_widgets.values(),
        *scr._streamed_thoughts,
        *(w for ws in scr._submit_solution_widgets for w in ws),
        *map(_bound_widget, scr._held_reveals),
        *scr._question_rows.values(),
        *scr._window_heads.values(),
        scr._live_mark,
        scr._fresh_line,
    ]
    for views in (scr._block_views, scr._history_views):
        for view in views.values():
            held += [*view._widgets.values(), *view._issued]
    # Память эфемерного держит копии снятого, не сами снятые виджеты.
    ephemera = scr._ephemera
    held += [e.widget for e in ephemera._entries]
    held += [d.owner for d in ephemera._decor]
    return held


def _turn_subtree(scr, turn: int) -> set:
    return {
        w
        for top in scr._window_turn_widgets(turn)
        for w in top.walk_children(with_self=True)
    }


@pytest.mark.asyncio
async def test_registries_empty_after_forget_live_turn(tmp_path):
    """Ход живого пути (вложенный трейс, мысли, «!», вопрос, вызовы)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        gone = _turn_subtree(scr, 0)
        assert gone & set(_held(scr))
        assert scr.forget_turn(0)
        await pilot.pause()
        assert not gone & set(_held(scr))
        assert all(not w.is_attached for w in gone)


@pytest.mark.asyncio
async def test_registries_empty_after_forget_history_turns(tmp_path):
    """Ходы отрисовщика кусков (виды блоков кусков, сжатие, вопросы)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = pilot.app.screen
        write_history(str(tmp_path / "t"), load_history_fixture("compaction"))
        assert scr.attach_active_task(HISTORY_THREAD, is_resume=True)
        await pilot.pause()
        assert scr._summary_marker_widgets
        last = scr._feed_history.turn_count() - 1
        gone: set = set()
        for turn in range(scr._window_first, last):
            gone |= _turn_subtree(scr, turn)
            assert scr.forget_turn(turn)
        await pilot.pause()
        assert gone
        assert not gone & set(_held(scr))


@pytest.mark.asyncio
async def test_live_turns_never_forgotten(tmp_path):
    """Не снимаются: текущий (и не верхний) ход, ход со строкой в фокусе,
    ход с живой строкой (идущий вызов)."""
    async with _app(tmp_path / "t").run_test(size=_SIZE) as pilot:
        scr = await live_session(pilot)
        last = scr._feed_history.turn_count() - 1
        assert last == 1
        assert not scr.forget_turn(last)
        focused = scr._window_turn_widgets(0)[0]
        focused.can_focus = True
        focused.focus()
        await pilot.pause()
        assert not scr.forget_turn(0)
        scr.set_focus(None)
        row = next(iter(scr._subagent_parent_rows.values()))
        scr._active_calls[row.call_id] = row
        assert not scr.forget_turn(0)
        del scr._active_calls[row.call_id]
        assert scr.forget_turn(0)
