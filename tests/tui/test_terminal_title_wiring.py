import pytest


@pytest.mark.asyncio
async def test_new_resets_terminal_title(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.services import terminal_title as tt
    from gc_tui.app import TuiApp

    calls = []
    monkeypatch.setattr(tt, "set_terminal_title", lambda text, **k: calls.append(text))
    # main.py импортирует символы из модуля внутри функций — патчим там же:
    from gc_tui.screens import main as main_mod
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
    from gc_tui.app import TuiApp
    from gc_tui.screens import main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._last_user_send_text = ""        # resume — в этом запуске ничего не слали
        scr._resumed_prompt = "что ты умеешь?"   # кэш промпта сессии (attach прочитал из стора)
        monkeypatch.setattr(scr, "_session_status_word", lambda: "completed")
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ что ты умеешь?", calls


@pytest.mark.asyncio
async def test_title_status_is_live_and_deduped(tmp_path, monkeypatch):
    """Статус в заголовке обновляется в прогоне ([completed]→[running]); повторный
    вызов с тем же заголовком НЕ пишет OSC (дедуп — поллер зовёт каждый тик)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens import main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        scr._active_task_id = "t1"
        scr._resumed_prompt = "p"
        scr._last_user_send_text = ""
        badge = {"v": "completed"}
        monkeypatch.setattr(scr, "_session_status_word", lambda: badge["v"])

        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ p"
        calls.clear()
        scr._update_terminal_title()              # тот же статус → дедуп, без записи
        assert calls == []
        badge["v"] = "running"                     # статус сменился (старт прогона)
        scr._update_terminal_title()
        assert calls == ["✦ GigaCode ▸ p"]


@pytest.mark.asyncio
async def test_title_single_source_no_cross_session_leak(tmp_path, monkeypatch):
    """Единый источник: заголовок = _resumed_prompt (последнее сообщение активной
    сессии). attach(is_resume) перезаписывает его из стора → промпт прошлой сессии
    не прилипает к табу новой."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.screens import main as main_mod

    calls = []
    monkeypatch.setattr(main_mod, "set_terminal_title", lambda text, **k: calls.append(text), raising=False)

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(scr, "_session_status_word", lambda: "completed")
        scr._active_task_id = "A"
        scr._resumed_prompt = "запрос A"
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ запрос A"
        scr._active_task_id = "B"
        scr._resumed_prompt = "вопрос B"
        scr._update_terminal_title()
        assert calls[-1] == "✦ GigaCode ✓ вопрос B", calls


@pytest.mark.asyncio
async def test_attach_resume_overwrites_prompt_from_store(tmp_path, monkeypatch):
    """attach(is_resume=True) АВТОРИТЕТНО ставит _resumed_prompt из стора (даже None),
    чтобы промпт прошлой сессии не оставался; auto-create (is_resume=False) НЕ затирает
    свежий _resumed_prompt, если стор ещё пуст."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        # переключение на сессию, у которой в сторе есть последнее сообщение
        scr._resumed_prompt = "промпт прошлой сессии"
        monkeypatch.setattr(scr, "_session_last_prompt", lambda: "промпт новой")
        scr.attach_active_task("new-1", is_resume=True)
        await pilot.pause()
        assert scr._resumed_prompt == "промпт новой"

        # auto-create: только что отправили (_resumed_prompt=текст), стор ещё пуст
        scr._resumed_prompt = "только что отправил"
        monkeypatch.setattr(scr, "_session_last_prompt", lambda: None)
        scr.attach_active_task("fresh-1", is_resume=False)
        await pilot.pause()
        assert scr._resumed_prompt == "только что отправил"   # не затёрт
