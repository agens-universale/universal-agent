"""Main-screen header показывает: Сессия:id [running] модель:M · backend  |  F1 help · gc-ua."""
import pytest


@pytest.mark.asyncio
async def test_header_shows_task_model_backend(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path), system="cli",
        config={"model": "CodeAgent", "backend_api_mode": "chat_completions"},
    )
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        # Симулируем «активная задача»: attach_active_task
        (tmp_path / "meta_cli_7f3a9b1c.json").write_text(
            '{"thread_id":"7f3a9b1c","status":"running","updated_at":"2026-05-21T12:00:00+00:00","messages_count":1}',
            encoding="utf-8",
        )
        (tmp_path / "data_cli_7f3a9b1c.jsonl").write_text("", encoding="utf-8")
        scr.attach_active_task("7f3a9b1c")
        await pilot.pause()

        left = pilot.app.query_one("#header-left")
        right = pilot.app.query_one("#header-right")
        header_text = str(left.render()) + " " + str(right.render())
        assert "Сессия: 7f3a9b1c" in header_text
        assert "[running]" in header_text
        assert "CodeAgent" in header_text
        assert "chat" in header_text  # короткое имя backend'а


@pytest.mark.asyncio
async def test_attach_active_task_mirrors_into_tui_state(tmp_path):
    """attach_active_task должен синхронизировать tui_state.active_task_id."""
    pytest.importorskip("textual")
    from tui.app import TuiApp

    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M"})
    (tmp_path / "meta_cli_t1.json").write_text(
        '{"thread_id":"t1","status":"stopped","updated_at":"...","messages_count":0}',
        encoding="utf-8",
    )
    (tmp_path / "data_cli_t1.jsonl").write_text("", encoding="utf-8")

    async with app.run_test() as pilot:
        assert app.tui_state.active_task_id is None
        pilot.app.screen.attach_active_task("t1")
        await pilot.pause()
        assert app.tui_state.active_task_id == "t1"
