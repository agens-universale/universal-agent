"""Ctrl+V (action_paste) и правый клик вставляют из системного буфера."""

from gc_testkit.tui_support import tui_settings
import pytest


@pytest.mark.asyncio
async def test_action_paste_inserts_from_os_clipboard(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services import os_ops as os_ops

    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "из буфера")

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        await pilot.pause(0.1)
        inp.action_paste()
        await pilot.pause(0.1)
        assert inp.text == "из буфера"


@pytest.mark.asyncio
async def test_right_click_pastes_from_os_clipboard(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.services import os_ops as os_ops
    from textual import events

    monkeypatch.setattr(os_ops, "read_clipboard", lambda: "правый клик")

    app = TuiApp(
        threads_dir=str(tmp_path),
        system="cli",
        settings=tui_settings({"agent.model": "M"}),
    )
    async with app.run_test() as pilot:
        inp = pilot.app.query_one("#cmdinput")
        inp.focus()
        await pilot.pause(0.1)
        await inp._on_mouse_down(
            events.MouseDown(
                widget=inp,
                x=0,
                y=0,
                delta_x=0,
                delta_y=0,
                button=3,
                shift=False,
                meta=False,
                ctrl=False,
                screen_x=0,
                screen_y=0,
            )
        )
        await pilot.pause(0.1)
        assert inp.text == "правый клик"
