"""Error-row рендерится одним Static, не Horizontal-row из нескольких."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_command_error_is_single_static(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.containers import Horizontal
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/tst"
        await pilot.press("enter")
        await pilot.pause(0.1)

        trace = pilot.app.query_one("#trace")
        # #brand-row — легитимный контейнер бренд-карточки (логотип + колонка
        # статусов); его не считаем. Раньше исключение висело на классе
        # brand-tagline-row, которого давно нет, и тест проходил лишь потому, что в
        # узком тест-окне карточка собиралась вертикальным контейнером.
        rows = [h for h in trace.query(Horizontal) if h.id != "brand-row"]
        assert len(rows) == 0
        texts = [str(s.render()) for s in trace.query(Static)]
        assert any("неизвестная команда" in t and "/tst" in t for t in texts)
