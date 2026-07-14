import pytest
pytest.importorskip("textual")


@pytest.mark.asyncio
async def test_ctrl_c_idle_requests_exit_not_stop(tmp_path, monkeypatch):
    from tui.app import TuiApp
    app = TuiApp(threads_dir=str(tmp_path / "t"), config={"model": "M"})
    async with app.run_test() as pilot:
        scr = app.screen
        called = {"exit": 0, "stop": 0}
        monkeypatch.setattr(scr, "_request_exit", lambda: called.__setitem__("exit", called["exit"] + 1))
        monkeypatch.setattr(scr, "_cmd_stop", lambda: called.__setitem__("stop", called["stop"] + 1))
        monkeypatch.setattr(app, "_current_selection", lambda: "")
        app.action_interrupt()                       # нет run, нет выделения
        assert called == {"exit": 1, "stop": 0}      # exit-модалка, НЕ стоп/спам
