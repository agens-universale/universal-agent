"""confirm banner: показ/скрытие + перехват y/N/Esc → submit_confirm_reply."""
import pytest


class _StubRunner:
    """Минимальный runner-stub: фиксирует вызовы submit_confirm_reply."""

    def __init__(self) -> None:
        self.replies: list[bool] = []

    def is_running(self) -> bool:
        return True

    def submit_confirm_reply(self, ok: bool) -> None:
        self.replies.append(ok)


@pytest.mark.asyncio
async def test_confirm_banner_y_answers_true(tmp_path):
    """«y» при активном баннере: submit_confirm_reply(True), баннер снят."""
    pytest.importorskip("textual")
    from textual.containers import Horizontal
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        # Баннер смонтирован, input-row скрыт.
        assert scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 1
        assert pilot.app.query_one("#input-row", Horizontal).has_class("hidden")

        await pilot.press("y")
        await pilot.pause(0.1)

        assert runner.replies == [True]
        assert not scr._confirm_active
        assert scr._confirm_banner is None
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # input-row восстановлен
        assert not pilot.app.query_one("#input-row", Horizontal).has_class("hidden")


@pytest.mark.asyncio
async def test_confirm_banner_n_answers_false(tmp_path):
    """«n» при активном баннере: submit_confirm_reply(False)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("n")
        await pilot.pause(0.1)

        assert runner.replies == [False]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0


@pytest.mark.asyncio
async def test_confirm_banner_escape_answers_false(tmp_path):
    """Esc при активном баннере: submit_confirm_reply(False), не проваливается
    в глобальную Esc-лестницу (нет exit-модалки)."""
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.confirm_banner import ConfirmBanner

    runner = _StubRunner()
    app = TuiApp(threads_dir=str(tmp_path), system="cli",
                 config={"model": "M", "backend_api_mode": "chat_completions"})
    app.active_runner = runner

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.show_confirm_banner("Тест", "Разрешить?")
        await pilot.pause(0.1)

        await pilot.press("escape")
        await pilot.pause(0.1)

        assert runner.replies == [False]
        assert not scr._confirm_active
        assert len(pilot.app.query(ConfirmBanner)) == 0
        # Не открылась exit-модалка (screen_stack == 1)
        assert len(pilot.app.screen_stack) == 1
