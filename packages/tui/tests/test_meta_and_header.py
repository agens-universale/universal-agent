"""Task 6: мета-строка над вводом и новый хедер (Сессия:/welcome, без модели/статуса)."""

import pytest


@pytest.mark.asyncio
async def test_header_left_welcome_when_fresh(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    async with TuiApp(threads_dir=str(tmp_path / "threads")).run_test() as pilot:
        scr = pilot.app.screen
        left, _right = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        assert "Добро пожаловать в GigaCode" in s
        assert "модель" not in s and "контекст" not in s  # уехало в мету


@pytest.mark.asyncio
async def test_header_left_session_when_active(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    async with TuiApp(threads_dir=str(threads_dir)).run_test() as pilot:
        scr = pilot.app.screen
        (threads_dir / "meta_cli_abcd1234efgh.json").write_text(
            '{"thread_id":"abcd1234efgh","status":"stopped","updated_at":"2026-01-01T00:00:00+00:00","messages_count":0}',
            encoding="utf-8",
        )
        (threads_dir / "data_cli_abcd1234efgh.jsonl").write_text("", encoding="utf-8")
        scr.attach_active_task("abcd1234efgh")
        await pilot.pause()
        left, _ = scr._compose_header_segments()
        s = left if isinstance(left, str) else left.plain
        assert "Сессия:" in s and "abcd1234" in s


@pytest.mark.asyncio
async def test_meta_has_model_context_and_status(tmp_path):
    """footer-right содержит модель (мета-полоса удалена в Task 3; статус переезжает в Task 4)."""
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from textual.widgets import Static

    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    async with TuiApp(threads_dir=str(threads_dir)).run_test() as pilot:
        scr = pilot.app.screen

        # Мета-полоса удалена — виджетов #meta-* нет.
        assert not scr.query("#meta-left")
        assert not scr.query("#meta-right")

        # Модель из конфига в footer-right
        cfg_model = str((getattr(pilot.app, "_config", None) or {}).get("model", "?"))
        fr_text = str(scr.query_one("#footer-right", Static).render())
        assert cfg_model in fr_text, f"model {cfg_model!r} не найдена в footer-right: {fr_text!r}"

        # --- Активируем задачу ---
        (threads_dir / "meta_cli_abcd1234.json").write_text(
            '{"thread_id":"abcd1234","status":"stopped","updated_at":"2026-01-01T00:00:00+00:00","messages_count":0}',
            encoding="utf-8",
        )
        (threads_dir / "data_cli_abcd1234.jsonl").write_text("", encoding="utf-8")
        scr.attach_active_task("abcd1234")
        await pilot.pause()

        # Модель по-прежнему в footer-right после attach
        fr_text = str(scr.query_one("#footer-right", Static).render())
        assert cfg_model in fr_text, f"model {cfg_model!r} не найдена в footer-right после attach: {fr_text!r}"
