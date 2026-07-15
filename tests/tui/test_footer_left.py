"""Footer-left контекстно зависит от наличия задачи и активного run.

Idle: [F1]/help  [F2]/sessions  [F3]/diff  [F5]/config  [F6]/settings
Running: [F1]/help  [F2]/sessions  [F3]/diff  [Ctrl+C]/stop
(/skills и /export из футера убраны — доступны через палитру; /mcp не дублируем —
подсказка /mcp в бренд-карточке.)

[Esc] back/выход убраны — на main Esc контекстуален (clear/exit/stop),
поведение досказывается confirm-hint в footer-right.
"""
import json
from pathlib import Path

import pytest


def _write_task(tmp_path, tid):
    meta = {"thread_id": tid, "status": "stopped",
            "updated_at": "2026-05-28T12:00:00+00:00", "messages_count": 0}
    (Path(tmp_path) / f"meta_cli_{tid}.json").write_text(
        json.dumps(meta), encoding="utf-8",
    )
    (Path(tmp_path) / f"data_cli_{tid}.jsonl").write_text("", encoding="utf-8")


@pytest.mark.asyncio
async def test_footer_left_empty_state_no_back_no_stop_no_export():
    """Empty state: F-hotkeys chrome; без /mcp, [Esc], /stop, /export, Ctrl+a."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "[F1]/help" in rendered
        assert "[F2]/sessions" in rendered
        assert "[F3]/diff" in rendered
        assert "[F5]/config" in rendered
        assert "[F6]/settings" in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered
        assert "/stop" not in rendered
        assert "/export" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered
        assert "выход" not in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_left_task_idle_no_stop(tmp_path):
    """Task active, no active_runner → тот же набор команд, /stop НЕТ."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    tid = "idle1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.active_runner = None
        pilot.app.screen.attach_active_task(tid)
        await pilot.pause()

        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "[F2]/sessions" in rendered
        assert "[F1]/help" in rendered
        assert "[F5]/config" in rendered
        assert "[F3]/diff" in rendered
        assert "[F6]/settings" in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered
        assert "/export" not in rendered
        assert "/stop" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_left_task_running_shows_stop(tmp_path):
    """Task active + active_runner.is_running() → /stop вместо config/settings."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    class _FakeRunner:
        def is_running(self) -> bool: return True
        def stop(self) -> None: pass

    tid = "run1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        pilot.app.active_runner = _FakeRunner()
        # Footer обновляется в _poll_active каждые 250ms — подождём один тик.
        await pilot.pause(0.4)

        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "[Ctrl+C]/stop" in rendered
        assert "[F2]/sessions" in rendered
        assert "[F1]/help" in rendered
        assert "[F3]/diff" in rendered
        assert "/config" not in rendered
        assert "/settings" not in rendered
        assert "/mcp" not in rendered
        assert "/skills" not in rendered
        assert "/export" not in rendered
        assert "Esc" not in rendered
        assert "back" not in rendered


@pytest.mark.asyncio
async def test_footer_left_drops_stop_after_runner_finishes(tmp_path):
    """Когда runner перестаёт is_running() → /stop исчезает в следующий тик."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    class _FakeRunner:
        def __init__(self) -> None:
            self._running = True
        def is_running(self) -> bool: return self._running
        def stop(self) -> None: self._running = False

    tid = "trans1"
    _write_task(tmp_path, tid)
    app = TuiApp(threads_dir=str(tmp_path), system="cli", config={"model": "M"})
    async with app.run_test() as pilot:
        pilot.app.screen.attach_active_task(tid)
        runner = _FakeRunner()
        pilot.app.active_runner = runner
        await pilot.pause(0.4)
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "/stop" in rendered

        runner.stop()
        await pilot.pause(0.4)
        rendered = str(pilot.app.query_one("#footer-left", Static).render())
        assert "/stop" not in rendered
        assert "/sessions" in rendered
        assert "[F5]/config" in rendered
        assert "Ctrl+a" not in rendered


@pytest.mark.asyncio
async def test_footer_badge_counts_update_notice():
    """«Доступна новая версия» попадает в счётчик ФУТЕРА: он виден всегда.

    В карточке этот же факт показан отдельной строкой — там в счётчик он не идёт,
    иначе одно и то же утверждалось бы дважды. Футер такой строки не имеет: без
    счётчика признак исчезает вместе с карточкой при скролле."""
    pytest.importorskip("textual")
    from textual.widgets import Static
    from tui.app import TuiApp

    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        screen = pilot.app.screen_stack[0]
        screen._refresh_notices_footer()
        await pilot.pause()
        footer = str(pilot.app.query_one("#footer-left", Static).render())
        assert "notices" in footer          # счётчик появился

    # набор для футера содержит уведомление об обновлении, для карточки — нет
    async with TuiApp().run_test() as pilot:
        pilot.app._update_available = True
        footer_codes = {n.code for n in pilot.app.collect_footer_notices()}
        card_codes = {n.code for n in pilot.app.collect_unread_notices()}
        assert "update_available" in footer_codes
        assert "update_available" not in card_codes
