"""TUI: смена набора плагинов подхватывается на границе хода (§8.3)."""

import json
import shutil

import pytest
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.catalog import with_plugin_snapshot
from gc_core.runtime_notices import clear_runtime_notices
from gc_core.settings import runtime_of
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_testkit.settings import make_settings
from gc_testkit.tui_support import tui_settings

pytest.importorskip("textual")

from gc_tui import app as app_mod

HTTP = {"type": "http", "url": "https://e.x/mcp"}
HOOKS = {"SessionStart": [{"hooks": [{"type": "command", "command": "x"}]}]}
UNTRUSTED = "Хуки плагина hk из конфига проекта не запускаются."


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()
    clear_runtime_notices()
    yield
    clear_runtime_notices()


@pytest.fixture
def pushed(monkeypatch):
    calls = []
    monkeypatch.setattr(app_mod, "push_runtime_notice", calls.append)
    return calls


def _plugin(parent, name, *, servers=None, hooks=None):
    root = parent / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "skills" / "s").mkdir(parents=True)
    (root / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    if servers is not None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )
    if hooks is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8"
        )
    return root


def _app(settings):
    app = app_mod.TuiApp(settings=settings)
    app._executor_factory = lambda: None
    return app


def _started(config=None, runtime=None):
    """Настройки старта: снимок уже собран (как в `gc_tui.main`)."""
    loaded = tui_settings(config or {})
    if runtime:
        loaded = loaded.with_runtime(**runtime)
    return with_plugin_snapshot(loaded, clone=False)


def _skill_plugins(loaded):
    return {r.plugin for r in runtime_of(loaded).plugins.skill_roots}


def test_install_reaches_next_turn_whole(tmp_path):
    app = _app(_started())
    first = app.get_runner()

    plugin = _plugin(tmp_path / "src", "pl", servers={"srv": HTTP})
    installed.install(str(plugin))
    second = app.get_runner()

    loaded = app.effective_settings()
    assert second is not first
    assert _skill_plugins(loaded) == {"pl"}
    assert "pl_srv" in effective_mcp_servers(loaded)
    assert _skill_plugins(app.base_settings()) == {"pl"}


def test_unchanged_set_keeps_runner(tmp_path):
    installed.install(str(_plugin(tmp_path / "src", "pl")))
    app = _app(_started())
    base = app.base_settings()
    first = app.get_runner()

    assert app.get_runner() is first
    assert app.base_settings() is base


def test_running_runner_untouched_until_boundary(tmp_path):
    app = _app(_started())
    runner = app.get_runner()
    runner.is_running = lambda: True

    installed.install(str(_plugin(tmp_path / "src", "pl")))

    assert app.get_runner() is runner
    assert _skill_plugins(app.effective_settings()) == set()
    runner.is_running = lambda: False
    assert app.get_runner() is not runner
    assert _skill_plugins(app.effective_settings()) == {"pl"}


def test_session_profile_cache_gets_new_snapshot(tmp_path):
    app = _app(_started())
    app._session_overrides = {"profile": "x"}
    app._session_profile_cache = ((frozenset(), "x"), _started())
    app.get_runner()

    installed.install(str(_plugin(tmp_path / "src", "pl")))
    app.get_runner()

    assert _skill_plugins(app._session_profile_cache[1]) == {"pl"}
    assert _skill_plugins(app.effective_settings()) == {"pl"}


@pytest.mark.asyncio
async def test_palette_sees_plugin_skills_before_first_turn(tmp_path):
    from gc_tui.services.skills_scan import list_skills_off_loop

    installed.install(str(_plugin(tmp_path / "src", "pl")))
    app = _app(_started())

    ids = {s["id"] for s in await list_skills_off_loop(app.base_settings())}

    assert app.active_runner is None
    assert "pl:s" in ids


def test_change_tells_screen_to_drop_skills(tmp_path, monkeypatch):
    calls = []

    class _Screen:
        def drop_skills_cache(self):
            calls.append("drop")

    app = _app(_started())
    monkeypatch.setattr(app, "_main_screen", lambda: _Screen())
    app.get_runner()
    assert calls == []

    installed.install(str(_plugin(tmp_path / "src", "pl")))
    app.get_runner()

    assert calls == ["drop"]


def test_missing_source_at_rebuild_notice_without_clone(monkeypatch, pushed):
    clones = []
    monkeypatch.setattr(sources, "_clone", lambda *a: clones.append(a))
    source = {"git": "https://e.x/repo.git"}
    # Снимка ещё нет: отпечаток не совпадает.
    loaded = make_settings(
        {"plugins.sources": [source], "logging.save_logs": False}
    )
    app = _app(loaded)

    app.get_runner()

    assert clones == []
    assert [n.message for n in pushed] == [
        "Плагин из источника https://e.x/repo.git не подключён: "
        "репозиторий не получен."
    ]


def _hooked(tmp_path):
    """Каталог плагина с хуками из конфига проекта (недоверенный)."""
    path = tmp_path / "proj" / "hk"
    config = {"plugins.paths": [str(path)]}
    runtime = {"untrusted_plugin_entries": frozenset({str(path)})}
    return path, config, runtime


def _notice_count(loaded, head):
    return sum(n.startswith(head) for n in runtime_of(loaded).notices)


def test_rebuild_does_not_duplicate_startup_notices(tmp_path, pushed):
    path, config, runtime = _hooked(tmp_path)
    _plugin(path.parent, "hk", hooks=HOOKS)
    app = _app(_started(config, runtime))
    assert _notice_count(app.base_settings(), UNTRUSTED) == 1

    for name in ("a", "b"):
        installed.install(str(_plugin(tmp_path / "src", name)))
        app.get_runner()

    assert _notice_count(app.base_settings(), UNTRUSTED) == 1
    assert _notice_count(app.effective_settings(), UNTRUSTED) == 1
    assert pushed == []


def test_new_notice_pushed_once_per_process(tmp_path, pushed):
    path, config, runtime = _hooked(tmp_path)
    app = _app(_started(config, runtime))
    app.get_runner()

    _plugin(path.parent, "hk", hooks=HOOKS)
    app.get_runner()
    installed.install(str(_plugin(tmp_path / "src", "a")))
    app.get_runner()
    assert _notice_count(app.base_settings(), UNTRUSTED) == 0
    shutil.rmtree(path)
    app.get_runner()
    _plugin(path.parent, "hk", hooks=HOOKS)
    app.get_runner()

    assert [n.message[: len(UNTRUSTED)] for n in pushed] == [UNTRUSTED]
    assert _notice_count(app.base_settings(), UNTRUSTED) == 0


def _with_command(root, name="go"):
    (root / "commands").mkdir()
    (root / "commands" / f"{name}.md").write_text(
        "---\ndescription: d\n---\nBODY $ARGUMENTS\n", encoding="utf-8"
    )
    return root


class _Fake:
    def __init__(self, captured):
        self._captured = captured

    async def arun(self, messages, **kw):
        self._captured.append(list(messages))

    def stop_run(self):
        pass

    def add_message(self, m):
        pass


def _live_app(tmp_path, captured):
    app = app_mod.TuiApp(
        threads_dir=str(tmp_path / "threads"),
        system="cli",
        settings=_started({"agent.model": "M"}),
    )
    app._executor_factory = lambda: _Fake(captured)
    return app


async def _submit(pilot, text):
    inp = pilot.app.query_one("#cmdinput")
    inp.text = text
    await pilot.press("enter")
    await pilot.pause(0.3)


def test_refresh_plugin_set_skips_live_run(tmp_path):
    app = _app(_started())
    runner = app.get_runner()
    runner.is_running = lambda: True
    installed.install(str(_plugin(tmp_path / "src", "pl")))

    app.refresh_plugin_set()
    assert _skill_plugins(app.base_settings()) == set()

    runner.is_running = lambda: False
    app.refresh_plugin_set()
    assert _skill_plugins(app.base_settings()) == {"pl"}


@pytest.mark.asyncio
async def test_installed_command_accepted_on_first_submit(tmp_path):
    captured = []
    app = _live_app(tmp_path, captured)
    async with app.run_test() as pilot:
        installed.install(str(_with_command(_plugin(tmp_path / "src", "pl"))))
        await _submit(pilot, "/pl:go main.py")
        assert captured == [["/pl:go main.py"]]


@pytest.mark.asyncio
async def test_disabled_command_not_sent_to_model(tmp_path):
    captured = []
    installed.install(str(_with_command(_plugin(tmp_path / "src", "pl"))))
    app = _live_app(tmp_path, captured)
    async with app.run_test() as pilot:
        installed.disable("pl")
        await _submit(pilot, "/pl:go main.py")
        assert captured == []


@pytest.mark.asyncio
async def test_palette_open_refreshes_plugin_set(tmp_path):
    app = _live_app(tmp_path, [])
    async with app.run_test() as pilot:
        installed.install(str(_with_command(_plugin(tmp_path / "src", "pl"))))
        pilot.app.screen._open_palette(prefix="")
        commands = runtime_of(app.base_settings()).plugins.commands
        assert [(p, c.name) for p, c in commands] == [("pl", "go")]


@pytest.mark.asyncio
async def test_plugins_screen_after_external_uninstall(tmp_path):
    installed.install(str(_plugin(tmp_path / "src", "pl")))
    app = _live_app(tmp_path, [])
    async with app.run_test() as pilot:
        installed.uninstall("pl")
        pilot.app.screen._cmd_plugins()
        await pilot.pause()
        await app.workers.wait_for_complete()
        await pilot.pause()
        assert app.is_running
        assert _skill_plugins(app.screen._settings) == set()
        assert app.screen.cards == []


@pytest.mark.asyncio
async def test_installed_skill_runs_on_first_submit(tmp_path):
    captured = []
    app = _live_app(tmp_path, captured)
    async with app.run_test() as pilot:
        pilot.app.screen._skills_cache = []
        installed.install(str(_plugin(tmp_path / "src", "pl")))
        await _submit(pilot, "/pl:s main.py")
        assert len(captured) == 1
        assert "pl:s" in captured[0][0]


@pytest.mark.asyncio
async def test_start_header_counts_plugin_servers(tmp_path, monkeypatch):
    from gc_tui.widgets.start_header import StartHeader

    plugin = _plugin(tmp_path / "src", "pl", servers={"srv": HTTP})
    installed.install(str(plugin))
    app = _app(_started())
    totals: list[int] = []
    real_init = StartHeader.__init__

    def _init(self, *, mcp_total, **kwargs):
        totals.append(mcp_total)
        real_init(self, mcp_total=mcp_total, **kwargs)

    monkeypatch.setattr(StartHeader, "__init__", _init)
    async with app.run_test():
        pass
    assert totals == [1]
