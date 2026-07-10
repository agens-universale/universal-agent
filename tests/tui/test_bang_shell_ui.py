"""Smoke: bang-shell ветка submit не стартует агента и монтирует трейс."""
from __future__ import annotations

import pytest


def _trace_text(screen) -> str:
    from textual.widgets import Static
    from tui.widgets.trace_row import TraceRow

    parts: list[str] = []
    for row in screen.query(TraceRow):
        for st in row.query(Static):
            parts.append(str(st.render()))
    return "\n".join(parts)


def _trace_kinds(screen) -> list[str]:
    from tui.widgets.trace_row import TraceRow

    kinds: list[str] = []
    for row in screen.query(TraceRow):
        for cls in ("line-bang", "line-bang-out", "line-bang-fail"):
            if row.has_class(cls):
                kinds.append(cls)
    return kinds


@pytest.mark.asyncio
async def test_bang_echo_mounts_trace_not_agent(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    started = []

    class _FakeRunner:
        def is_running(self):
            return False

        def start(self, **kwargs):
            started.append(kwargs)

        def stop(self):
            pass

    app = TuiApp(threads_dir=str(tmp_path), config={"tui_bang_shell": {"enabled": True}})
    app._executor_factory = lambda: None

    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(pilot.app, "get_runner", lambda: _FakeRunner())
        scr.on_command_text_area_submitted(
            CommandTextArea.Submitted(value="! echo hello")
        )
        await pilot.pause()
        for _ in range(50):
            text = _trace_text(scr)
            if "hello" in text and "[exit" in text:
                break
            await pilot.pause(0.05)
        else:
            pytest.fail(f"bang output not in trace: {_trace_text(scr)!r}")

        assert started == []
        kinds = _trace_kinds(scr)
        assert "line-bang" in kinds
        assert "line-bang-out" in kinds


@pytest.mark.asyncio
async def test_bang_empty_no_spawn(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="!"))
        await pilot.pause()
        assert "пустая команда" in _trace_text(scr)


@pytest.mark.asyncio
async def test_bang_preflight_vim(tmp_path):
    pytest.importorskip("textual")
    from tui.app import TuiApp
    from tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! vim x"))
        await pilot.pause()
        text = _trace_text(scr)
        assert "$ vim x" in text
        assert "интерактив" in text.lower()
