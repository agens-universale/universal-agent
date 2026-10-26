"""Main-screen header показывает: хедер — Сессия:id | cwd; мета — модель · статус."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_header_shows_session_id_and_meta_shows_model(tmp_path):
    """После реструктуризации хрома: хедер = Сессия:id + cwd; мета = модель + статус."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings(
            {
                "agent.model": "CodeAgent",
                "backend.api_mode": "chat_completions",
            }
        ),
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

        # Хедер: Сессия:id слева, cwd (адаптивный) справа. Модель и статус — не в хедере.
        left = pilot.app.query_one("#header-left")
        header_left_text = str(left.render())
        assert "Сессия: 7f3a9b1c" in header_left_text
        assert "модель" not in header_left_text
        assert "[running]" not in header_left_text

        # Модель в footer-right (мета-полоса удалена в Task 3).
        footer_right = pilot.app.query_one("#footer-right", Static)
        assert "CodeAgent" in str(footer_right.render())


@pytest.mark.asyncio
async def test_attach_active_task_mirrors_into_tui_state(tmp_path):
    """attach_active_task должен синхронизировать tui_state.active_task_id."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
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
