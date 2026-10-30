"""`/плагин:команда` в TUI — обычный запрос ядру, без префикса скилла;
разрешение имени — функцией ядра по командам и скиллам (§4.2)."""

import threading

import pytest
from gc_core.plugins.manifest import PluginCommand
from gc_core.plugins.snapshot import PluginSnapshot
from gc_testkit.tui_support import tui_settings


def _cmd(name):
    return PluginCommand(
        name=name,
        description="",
        argument_hint="",
        allowed_tools=(),
        body="BODY $ARGUMENTS",
        source=f"/p/commands/{name}.md",
    )


class _Fake:
    def __init__(self, captured):
        self._captured = captured
        self._evt = threading.Event()

    async def arun(self, messages, **kw):
        self._captured.append(list(messages))
        self._evt.wait(timeout=2.0)

    def stop_run(self):
        self._evt.set()

    def add_message(self, m):
        pass


def _app(tmp_path, captured):
    from gc_tui.app import TuiApp

    settings = tui_settings({"agent.model": "M"}).with_runtime(
        plugins=PluginSnapshot(commands=(("plug", _cmd("review")),))
    )
    app = TuiApp(threads_dir=str(tmp_path), system="cli", settings=settings)
    app._executor_factory = lambda: _Fake(captured)
    return app


@pytest.mark.asyncio
@pytest.mark.parametrize("typed", ["/plug:review main.py", "/review main.py"])
async def test_tui_plugin_command_sent_as_plain_text(tmp_path, typed):
    pytest.importorskip("textual")
    captured = []
    app = _app(tmp_path, captured)
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = [{"id": "other"}]
        inp = pilot.app.query_one("#cmdinput")
        inp.text = typed
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert captured == [[typed]]


@pytest.mark.asyncio
async def test_tui_short_name_ambiguous_with_skill(tmp_path):
    pytest.importorskip("textual")
    captured = []
    app = _app(tmp_path, captured)
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "b:review"}]
        await scr._handle_command("review main.py")
        await pilot.pause(0.2)
        text = " ".join(
            str(w.render()) for w in scr.query(".line-error").results()
        )
        assert "/plug:review" in text and "/b:review" in text
        assert captured == []
