from datetime import UTC, datetime

import pytest
from gc_tui.models import TaskSummary


def _task(tid: str, updated_at: str) -> TaskSummary:
    return TaskSummary(thread_id=tid, status="stopped", updated_at=updated_at,
                       messages_count=1, skill_id=None, unread_count=0, last_error=None)


@pytest.mark.asyncio
async def test_mass_delete_keep_last_count_and_submit():
    pytest.importorskip("textual")
    from gc_tui.screens.mass_delete import MassDeleteScreen
    from textual.app import App
    from textual.widgets import Input, Static
    now = datetime(2026, 6, 6, tzinfo=UTC)
    tasks = [_task(f"t{i:02d}", f"2026-06-{5 - i:02d}T00:00:00+00:00") for i in range(5)]
    result = {}

    class _A(App):
        def on_mount(self):
            self.push_screen(MassDeleteScreen(tasks=tasks, now=now),
                             lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        scr.query_one("#mass-n", Input).value = "2"
        await pilot.pause()
        assert "3 из 5" in str(scr.query_one("#mass-count", Static).render())
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] == {"keep_last": 2}


@pytest.mark.asyncio
async def test_mass_delete_mode_toggle_to_older_than():
    pytest.importorskip("textual")
    from gc_tui.screens.mass_delete import MassDeleteScreen
    from textual.app import App
    from textual.widgets import Input
    now = datetime(2026, 6, 6, tzinfo=UTC)
    # старше 1 дня от 06-06 → 06-01 точно под удаление (count > 0, чтобы submit сработал)
    tasks = [_task("t0", "2026-06-01T00:00:00+00:00")]
    result = {}

    class _A(App):
        def on_mount(self):
            self.push_screen(MassDeleteScreen(tasks=tasks, now=now),
                             lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        scr.query_one("#mass-n", Input).value = "1"
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] == {"older_than_days": 1}


@pytest.mark.asyncio
async def test_mass_delete_escape_returns_none():
    pytest.importorskip("textual")
    from gc_tui.screens.mass_delete import MassDeleteScreen
    from textual.app import App
    now = datetime(2026, 6, 6, tzinfo=UTC)
    result = {"v": "unset"}

    class _A(App):
        def on_mount(self):
            self.push_screen(MassDeleteScreen(tasks=[], now=now),
                             lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        await pilot.press("escape")
        await pilot.pause()
    assert result["v"] is None


@pytest.mark.asyncio
async def test_mass_delete_superscript_digit_is_not_a_count():
    """Класс «isdigit + int»: `²` проходит `isdigit()`, а `int("²")` падает —
    и `_n()` зовётся на КАЖДОЙ отрисовке, так что диалог рушился прямо под
    пальцами. Ввод обязан читаться как «числа нет»: ноль под удаление."""
    pytest.importorskip("textual")
    from gc_tui.screens.mass_delete import MassDeleteScreen
    from textual.app import App
    from textual.widgets import Input, Static
    now = datetime(2026, 6, 6, tzinfo=UTC)
    tasks = [_task(f"t{i:02d}", f"2026-06-{5 - i:02d}T00:00:00+00:00") for i in range(5)]
    result = {"v": "unset"}

    class _A(App):
        def on_mount(self):
            self.push_screen(MassDeleteScreen(tasks=tasks, now=now),
                             lambda v: result.update(v=v))

    async with _A().run_test() as pilot:
        scr = pilot.app.screen
        scr.query_one("#mass-n", Input).value = "²"
        await pilot.pause()
        assert "0 из 5" in str(scr.query_one("#mass-count", Static).render())
        await pilot.press("enter")
        await pilot.pause()
    assert result["v"] == "unset"   # ничего не выбрано — диалог не закрывается
