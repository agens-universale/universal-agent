"""Превью сессии ведёт себя как живая лента: открывается у низа, «не внизу»
показывает плашку «в конец» (клик и End — к низу).
Enter остаётся «продолжить сессию»."""

import pytest
from gc_testkit.tui_support import tui_settings

LONG = "подними таймаут клиента и проверь тесты, " * 6


def _app(tmp_path):
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )


def _msg(mid, role, content):
    return {"payload": {"id": mid, "role": role, "content": content}}


def _agent(mid):
    return _msg(mid, "assistant", "\n".join(f"строка {i}" for i in range(40)))


def _segments():
    msgs = [
        _msg("u1", "user", "первая"),
        _agent("a1"),
        _msg("u2", "user", LONG),
        _agent("a2"),
    ]
    return [{"messages": msgs, "index": 0, "compacted_by": None}]


async def _open(pilot, result):
    from gc_tui.screens.preview import PreviewScreen

    screen = PreviewScreen(
        segments=_segments(), thread_id="abc12345", project_path="/p"
    )
    await pilot.app.push_screen(screen, lambda v: result.update(v=v))
    await pilot.pause(0.4)
    return screen


def _trace(scr):
    from textual.containers import VerticalScroll

    return scr.query_one("#trace", VerticalScroll)


def _overlays(scr):
    from gc_tui.widgets.trace_overlays import ScrollEndPill, StickyReply

    return scr.query_one(StickyReply), scr.query_one(ScrollEndPill)


async def _scroll(pilot, scr, y):
    _trace(scr).scroll_to(y=y, animate=False)
    await pilot.pause()
    await pilot.pause()


def _at_bottom(scr):
    trace = _trace(scr)
    return trace.max_scroll_y > 0 and trace.scroll_y == trace.max_scroll_y


@pytest.mark.asyncio
async def test_preview_opens_at_bottom(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _open(pilot, {})
        sticky, pill = _overlays(scr)
        assert _at_bottom(scr)
        assert scr._stick_bottom is True
        assert sticky.display is False
        assert pill.display is False


@pytest.mark.asyncio
async def test_preview_scroll_up_shows_click_pill(tmp_path):
    from gc_tui.widgets.trace_overlays import PILL_CLICK

    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _open(pilot, {})
        await _scroll(pilot, scr, 10)
        _, pill = _overlays(scr)
        assert scr._stick_bottom is False
        assert pill.display is True
        assert pill.label == PILL_CLICK
        # плашка — в последнем ряду ленты, над футером превью
        assert pill.region.y == _trace(scr).region.bottom - 1


@pytest.mark.asyncio
async def test_preview_pill_click_goes_to_end(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _open(pilot, {})
        await _scroll(pilot, scr, 10)
        _, pill = _overlays(scr)
        await pilot.click(pill)
        await pilot.pause(0.2)
        assert _at_bottom(scr)
        assert scr._stick_bottom is True
        assert pill.display is False


@pytest.mark.asyncio
async def test_preview_end_key_goes_to_end(tmp_path):
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _open(pilot, {})
        await _scroll(pilot, scr, 10)
        await pilot.press("end")
        await pilot.pause(0.2)
        assert _at_bottom(scr)
        assert scr._stick_bottom is True


@pytest.mark.asyncio
async def test_preview_enter_still_resumes_when_scrolled_up(tmp_path):
    result = {}
    async with _app(tmp_path).run_test(size=(100, 30)) as pilot:
        scr = await _open(pilot, result)
        await _scroll(pilot, scr, 10)
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] is True
