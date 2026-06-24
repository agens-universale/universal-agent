import pytest


@pytest.mark.asyncio
async def test_new_resets_terminal_title(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    import tui.services.terminal_title as tt
    from tui.app import TuiApp

    calls = []
    monkeypatch.setattr(tt, "set_terminal_title", lambda text, **k: calls.append(text))
    # main.py импортирует символы из модуля внутри функций — патчим там же:
    import tui.screens.main as main_mod
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._on_user_text_submitted("какой-то предыдущий промпт")
        await pilot.pause()
        calls.clear()
        scr._cmd_new()
        await pilot.pause()
        assert any(c == "✦ GigaCode" for c in calls), f"ожидали сброс заголовка, calls={calls}"


@pytest.mark.asyncio
async def test_resume_title_uses_session_last_prompt(tmp_path, monkeypatch):
    """При возобновлении сессии (в этом запуске не слали) заголовок берёт последний
    промпт сессии из стора + её статус, а не пустоту."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._last_user_send_text = ""   # resume — в этом запуске ничего не слали
        monkeypatch.setattr(scr, "_session_last_prompt", lambda: "что ты умеешь?")
        monkeypatch.setattr(scr, "_task_status_badge", lambda: "[completed]")
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode · [completed] · что ты умеешь?", calls
