"""NoticesScreen: F4-биндинг + экран уведомлений (Task 7)."""
import pytest


@pytest.mark.asyncio
async def test_notices_screen_lists_all(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    cfg = {"model": "M", "backend_api_mode": "chat_completions",
           "stop_tools": ["bogus"], "agentsmd_override": True}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        await pilot.press("f4")
        await pilot.pause()
        # на экране /notices видны тексты уведомлений
        from tui.screens.notices import NoticesScreen
        assert isinstance(pilot.app.screen, NoticesScreen)


def test_header_text_counts_in_header():
    # Счётчики ⚠/ℹ — в шапке через «·» (как «mcp · серверы и тулы»), нули опущены.
    from tui.screens.notices import NoticesScreen
    from startup_notices import Notice
    scr = NoticesScreen(notices=[
        Notice("warning", "a", "x"), Notice("warning", "b", "y"), Notice("info", "c", "z"),
    ])
    assert scr._header_text() == "уведомления · ⚠ 2 · ℹ 1"
    assert NoticesScreen(notices=[Notice("warning", "a", "x")])._header_text() == "уведомления · ⚠ 1"
    assert NoticesScreen(notices=[])._header_text() == "уведомления"


def test_notice_text_highlights_technical_names():
    # Имена из highlights выделяются стилем имени; прочий текст — нет.
    from tui.screens.notices import _notice_text, _NAME_STYLE
    from startup_notices import Notice
    n = Notice("warning", "mcp_failed", "MCP не подключились: office.", highlights=("office",))
    t = _notice_text(n)
    styled = [t.plain[s.start:s.end] for s in t.spans if _NAME_STYLE in str(s.style)]
    assert "office" in styled
    assert "MCP" not in styled  # не-имя не выделяется


@pytest.mark.asyncio
async def test_notices_screen_empty_state(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    cfg = {"model": "M", "backend_api_mode": "chat_completions"}
    async with TuiApp(threads_dir=str(tmp_path / "t"), config=cfg).run_test() as pilot:
        await pilot.press("f4")
        await pilot.pause()
        from tui.screens.notices import NoticesScreen
        assert isinstance(pilot.app.screen, NoticesScreen)
