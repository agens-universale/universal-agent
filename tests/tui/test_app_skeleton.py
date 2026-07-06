"""TuiApp поднимается, рисует empty-state (бренд) и принимает /quit."""
import pytest


@pytest.mark.asyncio
async def test_app_starts_and_renders_empty_state():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        left = pilot.app.query_one("#header-left")
        right = pilot.app.query_one("#header-right")
        header_text = str(left.render()) + " " + str(right.render())
        # Хедер-слева в empty-state — welcome-строка (модель уехала в мету).
        assert "GigaCode" in header_text
        assert "Добро пожаловать" in header_text
        inp = pilot.app.query_one("#cmdinput")
        assert inp.has_focus


@pytest.mark.asyncio
async def test_app_quits_on_slash_quit():
    pytest.importorskip("textual")
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/quit"
        await pilot.press("enter")
        # после /quit приложение завершается; pilot завершит контекст
