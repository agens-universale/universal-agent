"""Парсинг /команд + inline-ошибка с closest-match (§4)."""
import pytest


@pytest.mark.asyncio
async def test_unknown_command_shows_closest_match():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/statuz"
        await pilot.press("enter")
        # Должна появиться inline-ошибка [!] с closest
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text
        assert "/statuz" in text
        assert "вы имели в виду" in text


@pytest.mark.asyncio
async def test_unknown_command_no_close_match():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/zzzzzz9999"
        await pilot.press("enter")
        trace = pilot.app.query_one("#trace")
        text = "\n".join(str(s.render()) for s in trace.query("Static"))
        assert "[!]" in text
        assert "вы имели в виду" not in text
