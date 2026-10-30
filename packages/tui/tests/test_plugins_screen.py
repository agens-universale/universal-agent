"""Экран /plugins: карточки установленных плагинов (read-only)."""

from pathlib import Path

import pytest
from gc_core.plugins.manifest import HookCommand, HookMatcher
from gc_testkit.tui_support import as_settings, tui_settings
from textual.widgets import Static


def _card(name, enabled=True, **kw):
    base = {
        "name": name,
        "version": "1.0.0",
        "format": "claude-code",
        "source": "o/r@abcdef12",
        "enabled": enabled,
        "skills": [f"{name}:review"],
        "commands": ["go"],
        "hooks": ["PreToolUse Bash"],
        "mcp_servers": ["srv"],
        "roles": [
            {
                "name": "helper",
                "status": "все инструменты · недоступна: субагенты "
                "выключены: включите agent.use_local_subagents",
                "ok": False,
                "notes": [],
            }
        ],
        "warnings": ["хуки Stop будут поддержаны после middleware"],
        "info": ["MCP-сервер srv плагина one заменён записью one_srv"],
        "error": "",
    }
    if not enabled:
        base.update(
            skills=[],
            commands=[],
            hooks=[],
            mcp_servers=[],
            roles=[],
            warnings=[],
            info=[],
        )
    base.update(kw)
    return base


async def _open(tmp_path, monkeypatch, cards, notices=()):
    from gc_tui.app import TuiApp
    from gc_tui.screens import plugins as mod

    monkeypatch.setattr(mod, "plugin_cards", lambda settings: cards)
    monkeypatch.setattr(mod, "general_notices", lambda s: list(notices))
    app = TuiApp(
        threads_dir=str(tmp_path), system="cli", settings=tui_settings({})
    )
    return app, mod.PluginsScreen(settings=as_settings({}))


async def _push(app, pilot, scr):
    await app.push_screen(scr)
    await app.workers.wait_for_complete()
    await pilot.pause()


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
        await _push(app, pilot, scr)
        assert len(scr.cards) == 2
        assert app.focused is scr.cards[0]
        assert "✓" in scr.cards[0]._renderable().plain
        assert "✗" in scr.cards[1]._renderable().plain
        assert "o/r@abcdef12" in scr.cards[0]._renderable().plain


@pytest.mark.asyncio
async def test_plugins_screen_expand_shows_components_roles_and_problems(
    tmp_path, monkeypatch
):
    app, scr = await _open(tmp_path, monkeypatch, [_card("one")])
    async with app.run_test() as pilot:
        await _push(app, pilot, scr)
        collapsed = _text(scr)
        assert "⚠ 2" in collapsed  # хуки Stop + недоступная роль
        assert "one:review" not in collapsed
        await pilot.press("enter")
        text = _text(scr)
        assert "one:review" in text
        assert "PreToolUse" in text
        assert "роль helper: все инструменты · недоступна:" in text
        assert "use_local_subagents" in text
        assert "⚠ хуки Stop будут поддержаны после middleware" in text
        assert "· MCP-сервер srv плагина one заменён записью" in text
        await pilot.press("space")
        assert "one:review" not in _text(scr)


@pytest.mark.asyncio
async def test_plugins_screen_info_alone_is_not_a_warning_badge(
    tmp_path, monkeypatch
):
    card = _card("one", roles=[], warnings=[])
    app, scr = await _open(tmp_path, monkeypatch, [card])
    async with app.run_test() as pilot:
        await _push(app, pilot, scr)
        assert "⚠" not in _text(scr)


@pytest.mark.asyncio
async def test_plugins_screen_empty_state_and_general_notices(
    tmp_path, monkeypatch
):
    note = "Плагины выключены: задан paths.skills_data_path."
    app, scr = await _open(tmp_path, monkeypatch, [], [note])
    async with app.run_test() as pilot:
        await _push(app, pilot, scr)
        assert scr.cards == []
        shown = " ".join(str(w.render()) for w in scr.query(Static))
        assert "gc-plugin install <owner/repo>" in shown
        assert f"⚠ {note}" in shown


def _snapshot_settings(plugins, notices=(), roles=()):
    from gc_core.plugins.snapshot import PluginSnapshot

    snap = PluginSnapshot(
        plugins=tuple(plugins), notices=tuple(notices), roles=tuple(roles)
    )
    return as_settings({}).with_runtime(plugins=snap)


def _no_entries(monkeypatch, mod, paths=()):
    from gc_core.plugins.catalog import PluginEntries

    monkeypatch.setattr(
        mod,
        "plugin_entries",
        lambda cfg, clone: PluginEntries(tuple(paths), frozenset(), ()),
    )


def test_plugin_cards_merges_loaded_and_disabled(tmp_path, monkeypatch):
    from gc_core.plugins import installed
    from gc_core.plugins.manifest import Plugin
    from gc_tui.services import plugin_cards as mod

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
            (inst / "a" / "skills",),
            hooks={"Stop": (hook,)},
        ),
        Plugin("cfg", "2.0", Path("/elsewhere/cfg"), ()),
    )
    _no_entries(monkeypatch, mod)
    monkeypatch.setattr(
        mod,
        "list_installed",
        lambda: [
            installed.InstalledPlugin("a", "0", "o/r", commit="1234567890"),
            installed.InstalledPlugin("off", "1", "o/off", enabled=False),
        ],
    )
    cards = {
        c["name"]: c for c in mod.plugin_cards(_snapshot_settings(loaded))
    }
    assert cards["a"]["version"] == "—"
    assert cards["a"]["source"] == "o/r@12345678"
    assert cards["a"]["skills"] == ["a:s1"]
    assert cards["a"]["hooks"] == ["Stop Bash"]
    assert cards["cfg"]["source"] == "config"
    assert cards["off"]["enabled"] is False
    assert cards["off"]["skills"] == []


def test_plugin_cards_shows_plugins_that_failed_to_load(tmp_path, monkeypatch):
    from gc_core.plugins import installed
    from gc_tui.screens.plugins import PluginCard
    from gc_tui.services import plugin_cards as mod

    inst = tmp_path / "inst"
    (inst / "b").mkdir(parents=True)
    broken = tmp_path / "broken"
    broken.mkdir()
    monkeypatch.setattr(installed, "installed_dir", lambda: inst)
    _no_entries(monkeypatch, mod, [str(broken), str(inst / "b")])
    monkeypatch.setattr(mod, "load_plugin", lambda path: None)
    monkeypatch.setattr(
        mod,
        "list_installed",
        lambda: [installed.InstalledPlugin("b", "1", "o/b")],
    )
    cards = {c["name"]: c for c in mod.plugin_cards(_snapshot_settings(()))}
    assert cards["broken"]["source"] == "config"
    assert cards["b"]["source"] == "o/b"
    for name in ("broken", "b"):
        text = PluginCard(cards[name])._renderable().plain
        assert "✗" in text and "не загружен (см. лог)" in text


def _agent(name, **kw):
    from gc_core.plugins.manifest import PluginAgent

    return PluginAgent(
        name=name,
        description="",
        tools=None,
        body="",
        source=f"/p/agents/{name}.md",
        **kw,
    )


def _role(agent, tools=None, mcp=()):
    from gc_core.tools.subagents.custom.file_definitions import (
        SubagentDefinition,
    )

    return SubagentDefinition(
        name=f"subagent_p_{agent}",
        description="",
        role="",
        tools=tools,
        stop_tools=(),
        source_path=f"/p/agents/{agent}.md",
        mcp_allow=mcp,
    )


def test_plugin_card_lists_unsupported_and_mcp_problems(monkeypatch):
    from gc_core.plugins.manifest import Plugin
    from gc_tui.services import plugin_cards as mod

    _no_entries(monkeypatch, mod)
    monkeypatch.setattr(mod, "list_installed", list)
    plugin = Plugin(
        "p",
        "1",
        Path("/p"),
        (),
        mcp_problems=("mcp bad: пропущен (env var X is not set)",),
        hook_problems=("хуки Stop будут поддержаны после middleware",),
    )
    notices = [
        "MCP-сервер dup плагина p (p_dup) пропущен: ключ уже занят "
        "плагином q.",
        "MCP-сервер srv плагина p заменён записью p_srv из mcp.servers.",
        "MCP-сервер srv плагина pq (pq_srv) пропущен: неверные поля: x.",
        "Команда плагина p:go не подключена: есть skill с тем же именем.",
        "Плагин из источника https://e.x/r.git не подключён: репозиторий "
        "не получен.",
    ]
    settings = _snapshot_settings([plugin], notices)
    (card,) = mod.plugin_cards(settings)
    assert card["warnings"] == [
        "хуки Stop будут поддержаны после middleware",
        "mcp bad: пропущен (env var X is not set)",
        notices[0],
        notices[3],
    ]
    assert card["info"] == [notices[1]]
    assert mod.general_notices(settings) == [notices[4]]


def test_plugin_card_roles_availability_rights_and_broken_header(
    monkeypatch,
):
    from gc_core.plugins.manifest import Plugin
    from gc_core.tools.subagents.role_info import RoleRights
    from gc_tui.services import plugin_cards as mod

    _no_entries(monkeypatch, mod)
    monkeypatch.setattr(mod, "list_installed", list)
    calls = []

    def availability(_settings, name):
        calls.append(name)
        return "превышен лимит" if name == "subagent_p_lim" else None

    rights = {
        "subagent_p_rw": RoleRights(True, True, False, True),
        "subagent_p_ro": RoleRights(False, False, False, False),
        "subagent_p_lim": RoleRights(True, True, True, False),
    }
    monkeypatch.setattr(mod, "role_availability", availability)
    monkeypatch.setattr(mod, "role_rights", lambda s, d: rights[d.name])
    agents = (
        _agent("rw"),
        _agent("ro"),
        _agent("lim"),
        _agent("bad", problem="шапка не разобралась", header_broken=True),
    )
    plugin = Plugin("p", "1", Path("/p"), (), agents=agents)
    notices = [
        "Роль p:rw: инструменты отброшены: Foo.",
        "Роль p:bad не подключена: шапка не разобралась.",
    ]
    settings = _snapshot_settings(
        [plugin], notices, [_role("rw"), _role("ro"), _role("lim")]
    )
    (card,) = mod.plugin_cards(settings)
    assert card["roles"] == [
        {
            "name": "rw",
            "status": "правка файлов, команды, MCP",
            "ok": True,
            "notes": ["инструменты отброшены: Foo."],
        },
        {"name": "ro", "status": "только чтение", "ok": True, "notes": []},
        {
            "name": "lim",
            "status": "все инструменты · недоступна: превышен лимит",
            "ok": False,
            "notes": [],
        },
        {
            "name": "bad",
            "status": "не подключена: шапка не разобралась.",
            "ok": False,
            "notes": [],
        },
    ]
    assert card["warnings"] == []
    assert calls == ["subagent_p_rw", "subagent_p_ro", "subagent_p_lim"]


def test_plugin_card_real_availability_when_subagents_off(monkeypatch):
    from gc_core.plugins.manifest import Plugin
    from gc_tui.services import plugin_cards as mod

    _no_entries(monkeypatch, mod)
    monkeypatch.setattr(mod, "list_installed", list)
    plugin = Plugin("p", "1", Path("/p"), (), agents=(_agent("rw"),))
    from gc_core.plugins.snapshot import PluginSnapshot

    settings = as_settings({"agent.use_local_subagents": False})
    settings = settings.with_runtime(
        plugins=PluginSnapshot(plugins=(plugin,), roles=(_role("rw"),))
    )
    (card,) = mod.plugin_cards(settings)
    assert card["roles"][0]["ok"] is False
    assert "субагенты выключены" in card["roles"][0]["status"]
