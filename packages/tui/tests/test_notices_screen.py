"""NoticesScreen: F4-биндинг + экран уведомлений (Task 7)."""

import pytest


@pytest.mark.asyncio
async def test_notices_screen_lists_all(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {
        "model": "M",
        "backend_api_mode": "chat_completions",
        "stop_tools": ["bogus"],
        "agentsmd_override": True,
    }
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), config=cfg
    ).run_test() as pilot:
        await pilot.press("f4")
        await pilot.pause()
        # на экране /notices видны тексты уведомлений
        from gc_tui.screens.notices import NoticesScreen

        assert isinstance(pilot.app.screen, NoticesScreen)


def test_header_text_counts_in_header():
    pytest.importorskip("textual")
    # Счётчики ⚠/ℹ — в шапке через «·» (как «mcp · серверы и тулы»), нули опущены.
    from gc_core.startup_notices import Notice
    from gc_tui.screens.notices import NoticesScreen

    notices = [
        Notice("warning", "a", "x"),
        Notice("warning", "b", "y"),
        Notice("info", "c", "z"),
    ]
    scr = NoticesScreen(notices=notices)
    from gc_tui.services.notices_ui import INFO_GLYPH, WARN_GLYPH

    # Значки берём из общего источника: в них вариационный селектор, и литерал в тесте
    # разъезжался бы с кодом по невидимому символу.
    assert (
        scr._header_text(notices)
        == f"уведомления · {WARN_GLYPH} 2  {INFO_GLYPH} 1"
    )
    assert (
        NoticesScreen(notices=[Notice("warning", "a", "x")])._header_text(
            [Notice("warning", "a", "x")]
        )
        == f"уведомления · {WARN_GLYPH} 1"
    )
    assert NoticesScreen(notices=[])._header_text([]) == "уведомления"


def test_notice_text_highlights_technical_names():
    pytest.importorskip("textual")
    # Имена из highlights выделяются стилем имени; прочий текст — нет.
    from gc_core.startup_notices import Notice
    from gc_tui.screens.notices import _NAME_STYLE, _notice_text

    n = Notice(
        "warning",
        "mcp_failed",
        "MCP не подключились: office.",
        highlights=("office",),
    )
    t = _notice_text(n)
    styled = [
        t.plain[s.start : s.end]
        for s in t.spans
        if _NAME_STYLE in str(s.style)
    ]
    assert "office" in styled
    assert "MCP" not in styled  # не-имя не выделяется


@pytest.mark.asyncio
async def test_notices_screen_empty_state(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    cfg = {"model": "M", "backend_api_mode": "chat_completions"}
    async with TuiApp(
        threads_dir=str(tmp_path / "t"), config=cfg
    ).run_test() as pilot:
        await pilot.press("f4")
        await pilot.pause()
        from gc_tui.screens.notices import NoticesScreen

        assert isinstance(pilot.app.screen, NoticesScreen)
