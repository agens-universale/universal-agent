"""Экран /plugins: карточки установленных плагинов (read-only)."""

from pathlib import Path

import pytest
from gc_core.plugins.manifest import HookCommand, HookMatcher
from gc_testkit.tui_support import as_settings, tui_settings


def _card(name, enabled=True, **kw):
    base = {
        "name": name,
        "version": "1.0.0",
        "format": "claude-code",
        "source": "o/r@abcdef12",
        "enabled": enabled,
        "skills": [f"{name}:review"],
        "commands": ["go"],
        "agents": ["helper"],
        "hooks": ["PreToolUse Bash"],
        "mcp_servers": ["srv"],
        "agents_off": True,
        "error": "",
    }
    if not enabled:
        base.update(
            skills=[], commands=[], agents=[], hooks=[], mcp_servers=[]
        )
        base["agents_off"] = False
    base.update(kw)
    return base


async def _open(tmp_path, monkeypatch, cards):
    from gc_tui.app import TuiApp
    from gc_tui.screens import plugins as mod

    monkeypatch.setattr(mod, "plugin_cards", lambda settings: cards)
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    return app, mod.PluginsScreen(settings=as_settings({}))


def _text(scr):
    return "\n".join(c._renderable().plain for c in scr.cards)


def test_plugins_command_registered():
    from gc_tui.commands import build_default_registry

    assert build_default_registry().lookup("plugins") is not None


@pytest.mark.asyncio
async def test_plugins_screen_lists_installed(tmp_path, monkeypatch):
    app, scr = await _open(
        tmp_path, monkeypatch, [_card("one"), _card("two", enabled=False)]
    )
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        assert len(scr.cards) == 2
        assert app.focused is scr.cards[0]
        assert "✓" in scr.cards[0]._renderable().plain
        assert "✗" in scr.cards[1]._renderable().plain
        assert "o/r@abcdef12" in scr.cards[0]._renderable().plain


@pytest.mark.asyncio
async def test_plugins_screen_expand_shows_components_and_agents_note(
    tmp_path, monkeypatch
):
    app, scr = await _open(tmp_path, monkeypatch, [_card("one")])
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        await pilot.press("enter")
        text = _text(scr)
        assert "one:review" in text
        assert "PreToolUse" in text
        assert "use_local_subagents" in text
        await pilot.press("space")
        assert "one:review" not in _text(scr)


@pytest.mark.asyncio
async def test_plugins_screen_empty_state(tmp_path, monkeypatch):
    app, scr = await _open(tmp_path, monkeypatch, [])
    async with app.run_test() as pilot:
        await app.push_screen(scr)
        await pilot.pause()
        assert scr.cards == []
        assert (
            "gc-plugin install <owner/repo>" in scr._empty_renderable().plain
        )


def test_plugin_cards_merges_loaded_and_disabled(tmp_path, monkeypatch):
    from gc_core.plugins import installed
    from gc_core.plugins.manifest import Plugin
    from gc_tui.screens import plugins as mod

    inst = tmp_path / "inst"
    (inst / "a" / "skills" / "s1").mkdir(parents=True)
    (inst / "a" / "skills" / "s1" / "SKILL.md").write_text("x")
    monkeypatch.setattr(installed, "installed_dir", lambda: inst)
    hook = HookMatcher("Bash", (HookCommand("echo"),))
    loaded = (
        Plugin(
            "a",
            "0",
            inst / "a",
            inst / "a" / "skills",
            hooks={"Stop": (hook,)},
        ),
        Plugin("cfg", "2.0", Path("/elsewhere/cfg"), None),
    )
    monkeypatch.setattr(mod, "configured_paths", lambda cfg: ["x"])
    monkeypatch.setattr(mod, "load_plugins", lambda paths: loaded)
    monkeypatch.setattr(
        mod,
        "list_installed",
        lambda: [
            installed.InstalledPlugin("a", "0", "o/r", commit="1234567890"),
            installed.InstalledPlugin("off", "1", "o/off", enabled=False),
        ],
    )
    cards = {c["name"]: c for c in mod.plugin_cards(as_settings({}))}
    assert cards["a"]["version"] == "—"
    assert cards["a"]["source"] == "o/r@12345678"
    assert cards["a"]["skills"] == ["a:s1"]
    assert cards["a"]["hooks"] == ["Stop Bash"]
    assert cards["cfg"]["source"] == "config"
    assert cards["off"]["enabled"] is False
    assert cards["off"]["skills"] == []


def test_plugin_cards_shows_plugins_that_failed_to_load(tmp_path, monkeypatch):
    from gc_core.plugins import installed
    from gc_tui.screens import plugins as mod

    inst = tmp_path / "inst"
    (inst / "b").mkdir(parents=True)
    broken = tmp_path / "broken"
    broken.mkdir()
    monkeypatch.setattr(installed, "installed_dir", lambda: inst)
    monkeypatch.setattr(
        mod, "configured_paths", lambda cfg: [str(broken), str(inst / "b")]
    )
    monkeypatch.setattr(mod, "load_plugins", lambda paths: ())
    monkeypatch.setattr(mod, "load_plugin", lambda path: None)
    monkeypatch.setattr(
        mod,
        "list_installed",
        lambda: [installed.InstalledPlugin("b", "1", "o/b")],
    )
    cards = {c["name"]: c for c in mod.plugin_cards(as_settings({}))}
    assert cards["broken"]["source"] == "config"
    assert cards["b"]["source"] == "o/b"
    for name in ("broken", "b"):
        text = mod.PluginCard(cards[name])._renderable().plain
        assert "✗" in text and "не загружен (см. лог)" in text
