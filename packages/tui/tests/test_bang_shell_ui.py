"""Smoke: bang-shell ветка submit не стартует агента и монтирует трейс."""
from __future__ import annotations

import pytest


def _trace_text(screen) -> str:
    from gc_tui.widgets.trace_row import TraceRow
    from textual.widgets import Static

    parts: list[str] = []
    for row in screen.query(TraceRow):
        for st in row.query(Static):
            parts.append(str(st.render()))
    return "\n".join(parts)


def _trace_kinds(screen) -> list[str]:
    from gc_tui.widgets.trace_row import TraceRow

    kinds: list[str] = []
    for row in screen.query(TraceRow):
        for cls in ("line-bang", "line-bang-out", "line-bang-fail"):
            if row.has_class(cls):
                kinds.append(cls)
    return kinds


@pytest.mark.asyncio
async def test_bang_echo_mounts_trace_not_agent(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

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
        await scr.on_command_text_area_submitted(
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
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="!"))
        await pilot.pause()
        assert "пустая команда" in _trace_text(scr)


@pytest.mark.asyncio
async def test_bang_preflight_vim(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    app = TuiApp(threads_dir=str(tmp_path))
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! vim x"))
        await pilot.pause()
        text = _trace_text(scr)
        assert "$ vim x" in text
        assert "интерактив" in text.lower()


async def _await_bang_done(pilot, scr, needle: str, *, rounds: int = 50) -> None:
    await pilot.pause()
    for _ in range(rounds):
        if needle in _trace_text(scr):
            return
        await pilot.pause(0.05)
    pytest.fail(f"{needle!r} not in trace: {_trace_text(scr)!r}")


async def _await_bang_gone(pilot, scr, *needles: str, rounds: int = 50) -> None:
    """Дождаться, пока блоки уйдут из трейса.

    remove_children() в Textual — отложенный prune (см. TraceScreen._clear_trace):
    одной pilot.pause() не хватает, и под нагрузкой полного прогона это читается
    как флап, хотя очистка отрабатывает штатно."""
    await pilot.pause()
    for _ in range(rounds):
        text = _trace_text(scr)
        if not any(n in text for n in needles):
            return
        await pilot.pause(0.05)
    pytest.fail(f"{needles!r} still in trace: {_trace_text(scr)!r}")


@pytest.mark.asyncio
async def test_bang_clear_all(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    class _FakeRunner:
        def is_running(self):
            return False

        def start(self, **kwargs):
            pass

        def stop(self):
            pass

    app = TuiApp(threads_dir=str(tmp_path), config={"tui_bang_shell": {"enabled": True}})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(pilot.app, "get_runner", lambda: _FakeRunner())
        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! echo one"))
        await _await_bang_done(pilot, scr, "one")
        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! echo two"))
        await _await_bang_done(pilot, scr, "two")
        assert "one" in _trace_text(scr) and "two" in _trace_text(scr)

        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! clear"))
        await _await_bang_gone(pilot, scr, "one", "two")
        text = _trace_text(scr)
        assert "one" not in text
        assert "two" not in text
        assert "$ clear" not in text
        assert _trace_kinds(scr) == []


@pytest.mark.asyncio
async def test_bang_clear_last_n(tmp_path, monkeypatch):
    pytest.importorskip("textual")
    from gc_tui.app import TuiApp
    from gc_tui.widgets.command_input import CommandTextArea

    class _FakeRunner:
        def is_running(self):
            return False

        def start(self, **kwargs):
            pass

        def stop(self):
            pass

    app = TuiApp(threads_dir=str(tmp_path), config={"tui_bang_shell": {"enabled": True}})
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        monkeypatch.setattr(pilot.app, "get_runner", lambda: _FakeRunner())
        for word in ("alpha", "beta", "gamma"):
            await scr.on_command_text_area_submitted(
                CommandTextArea.Submitted(value=f"! echo {word}")
            )
            await _await_bang_done(pilot, scr, word)

        await scr.on_command_text_area_submitted(CommandTextArea.Submitted(value="! clear 2"))
        await _await_bang_gone(pilot, scr, "beta", "gamma")
        text = _trace_text(scr)
        assert "alpha" in text
        assert "beta" not in text
        assert "gamma" not in text
