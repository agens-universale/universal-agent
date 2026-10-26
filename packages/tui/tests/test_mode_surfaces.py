"""Где ещё виден режим разрешений, кроме пилюли: `/config` и справка."""

import pytest
from gc_testkit.tui_support import tui_settings


def _app(tmp_path, **config):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp

    return TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M", **config}),
    )


async def _config_line(pilot) -> str:
    pilot.app.screen._cmd_config()
    await pilot.pause()
    return str(pilot.app.screen.query_one("#config-permission-mode").render())


@pytest.mark.asyncio
async def test_config_reports_mode_and_its_source(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        assert (
            await _config_line(pilot)
            == "режим разрешений: edit mode (по умолчанию)"
        )

    app = _app(tmp_path, **{"security.permissions.default_mode": "manual"})
    async with app.run_test() as pilot:
        assert (
            await _config_line(pilot)
            == "режим разрешений: manual mode (из конфига)"
        )


@pytest.mark.asyncio
async def test_config_reports_session_source_after_switch(tmp_path):
    async with _app(tmp_path).run_test() as pilot:
        await pilot.press("shift+tab")
        assert (
            await _config_line(pilot)
            == "режим разрешений: manual mode (выбран в этой сессии)"
        )


def test_help_lists_the_mode_hotkey_with_every_available_mode():
    from gc_core.security.permission_mode import AVAILABLE_MODES, mode_label
    from gc_tui.screens.help import _EXTRA_HOTKEYS

    rows = dict(_EXTRA_HOTKEYS)
    assert "Shift+Tab" in rows
    for mode in AVAILABLE_MODES:
        assert mode_label(mode.value) in rows["Shift+Tab"]
