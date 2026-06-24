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
        scr._last_user_send_text = ""        # resume — в этом запуске ничего не слали
        scr._resumed_prompt = "что ты умеешь?"   # кэш промпта сессии (attach прочитал из стора)
        monkeypatch.setattr(scr, "_task_status_badge", lambda: "[completed]")
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode · [completed] · что ты умеешь?", calls


@pytest.mark.asyncio
async def test_title_status_is_live_and_deduped(tmp_path, monkeypatch):
    """Статус в заголовке обновляется в прогоне ([completed]→[running]); повторный
    вызов с тем же заголовком НЕ пишет OSC (дедуп — поллер зовёт каждый тик)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._resumed_prompt = "p"
        scr._last_user_send_text = ""
        badge = {"v": "[completed]"}
        monkeypatch.setattr(scr, "_task_status_badge", lambda: badge["v"])

        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode · [completed] · p"
        calls.clear()
        scr._update_terminal_title()              # тот же статус → дедуп, без записи
        assert calls == []
        badge["v"] = "[running]"                   # статус сменился (старт прогона)
        scr._update_terminal_title()
        assert calls == ["✦ GigaCode · [running] · p"]


@pytest.mark.asyncio
async def test_title_send_text_scoped_to_its_session(tmp_path, monkeypatch):
    """Send-текст относится к своей сессии: после переключения на ДРУГУЮ сессию
    заголовок берёт её _resumed_prompt, а не прилипший промпт прошлой сессии."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    import tui.screens.main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(scr, "_task_status_badge", lambda: "[completed]")
        # отправили в сессии A
        scr._active_task_id = "A"
        scr._last_user_send_text = "запрос A"
        scr._last_send_task_id = "A"
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode · [completed] · запрос A"
        # переключились на B: send-поля от A остались, attach задал _resumed_prompt=B
        scr._active_task_id = "B"
        scr._resumed_prompt = "вопрос B"
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode · [completed] · вопрос B", calls
