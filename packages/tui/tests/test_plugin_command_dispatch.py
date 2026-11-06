"""`/плагин:команда` в TUI — обычный запрос ядру, без префикса скилла;
разрешение имени — функцией ядра по командам и скиллам (§4.2)."""

import threading

import pytest
from gc_core.plugins.catalog import plugin_fingerprint
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


def _app(tmp_path, captured, command="review"):
    from gc_tui.app import TuiApp

    settings = tui_settings({"agent.model": "M"})
    snap = PluginSnapshot(
        commands=(("plug", _cmd(command)),),
        fingerprint=plugin_fingerprint(settings),
    )
    settings = settings.with_runtime(plugins=snap)
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


@pytest.mark.asyncio
async def test_tui_pack_bootstrap_beats_skill_only_ambiguity(
    tmp_path, monkeypatch
):
    """Пакет ``review`` с bootstrap и скиллы ``x:review``/``y:review``:
    `/review` — bootstrap пакета, как до команд плагинов (команда плагина
    с другим именем)."""
    pytest.importorskip("textual")
    from gc_core.tools.skills import skill_delivery as sd

    async def _available(_skill_id, _reg):
        return True

    monkeypatch.setattr(sd, "skill_is_available", _available)
    captured = []
    app = _app(tmp_path, captured, command="other")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [
            {
                "id": "review:start",
                "pack_id": "review",
                "is_pack_bootstrap": True,
            },
            {"id": "x:review"},
            {"id": "y:review"},
        ]
        await scr._handle_command("review main.py")
        await pilot.pause(0.3)
        assert len(captured) == 1
        assert "review:start" in captured[0][0]


@pytest.mark.asyncio
async def test_tui_word_beats_plugin_command_with_same_short_name(tmp_path):
    """`/clear` — алиас `/new`: слово TUI сильнее команды плагина
    `plug:clear`, ядру ничего не уходит."""
    pytest.importorskip("textual")
    captured = []
    app = _app(tmp_path, captured, command="clear")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "other"}]
        new_calls = []
        scr._cmd_new = lambda: new_calls.append(True)
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/clear go"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert new_calls == [True]
        assert captured == []


@pytest.mark.asyncio
async def test_plugin_command_shadowed_by_tui_word_runs_by_full_id(tmp_path):
    pytest.importorskip("textual")
    captured = []
    app = _app(tmp_path, captured, command="clear")
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = [{"id": "other"}]
        inp = pilot.app.query_one("#cmdinput")
        inp.text = "/plug:clear go"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert captured == [["/plug:clear go"]]


@pytest.mark.asyncio
async def test_plugin_command_in_other_layout_reaches_core_translated(
    tmp_path,
):
    pytest.importorskip("textual")
    from gc_tui.services.keyboard_layout import swap_layout

    captured = []
    app = _app(tmp_path, captured)
    typed = swap_layout("review")
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = [{"id": "other"}]
        inp = pilot.app.query_one("#cmdinput")
        inp.text = f"/{typed} main.py"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert captured == [["/review main.py"]]


@pytest.mark.asyncio
async def test_plugin_command_known_to_highlight_and_space(tmp_path):
    """Подсветка и замена по пробелу знают команды плагинов — набранные
    как есть и в другой раскладке."""
    pytest.importorskip("textual")
    from gc_tui.services.keyboard_layout import swap_layout

    app = _app(tmp_path, [])
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "other"}]
        swapped = swap_layout("review")
        for name in ("review", "plug:review", swapped):
            assert scr._is_known_command(name)
            assert scr._is_plugin_command(name)
        assert scr._word_on_space(swapped) == "review"
        assert scr._word_on_space("review") is None


@pytest.mark.asyncio
async def test_tui_word_in_other_layout_beats_plugin_command(tmp_path):
    pytest.importorskip("textual")
    from gc_tui.services.keyboard_layout import swap_layout

    captured = []
    app = _app(tmp_path, captured, command="clear")
    async with app.run_test() as pilot:
        scr = pilot.app.screen
        scr._skills_cache = [{"id": "other"}]
        assert not scr._is_plugin_command(swap_layout("clear"))
        new_calls = []
        scr._cmd_new = lambda: new_calls.append(True)
        inp = pilot.app.query_one("#cmdinput")
        inp.text = f"/{swap_layout('clear')} go"
        await pilot.press("enter")
        await pilot.pause(0.3)
        assert new_calls == [True]
        assert captured == []
