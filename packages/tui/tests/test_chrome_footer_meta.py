import pytest


@pytest.mark.asyncio
async def test_meta_band_removed(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        assert not scr.query("#meta"), "полоса #meta должна быть удалена"
        assert not scr.query("#meta-left")
        assert not scr.query("#meta-right")


@pytest.mark.asyncio
async def test_footer_right_shows_model(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "MiniMaxAI/MiniMax-M2.7"}
        right = scr._compose_footer_right().plain
        # не chat_completions → только имя модели, без метра (+ точка-индикатор уровня)
        assert right == "● MiniMaxAI/MiniMax-M2.7"


@pytest.mark.asyncio
async def test_footer_right_model_meter_sep_compact(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from rich.text import Text

    app = TuiApp(threads_dir=str(tmp_path / "threads"))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        app._config = {"model": "M"}
        monkeypatch.setattr(scr, "_context_meter", lambda: Text("CTX"))
        assert scr._compose_footer_right().plain == "● M · CTX"


@pytest.mark.asyncio
async def test_footer_right_no_longer_gc_ua(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    async with TuiApp(
        threads_dir=str(tmp_path / "threads")
    ).run_test() as pilot:
        scr = pilot.app.screen
        fr = scr.query_one("#footer-right", Static)
        assert "gc-ua" not in fr.render().plain
