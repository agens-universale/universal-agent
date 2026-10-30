"""Снимок набора плагинов: состав, выключение по режимам, доверие, отпечаток."""

import json
import os
import shutil

import pytest
from gc_core.config import load_settings
from gc_core.config_modules.root import AppConfig
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.catalog import (
    build_plugin_snapshot,
    plugin_fingerprint,
    with_plugin_snapshot,
)
from gc_core.plugins.snapshot import PluginSnapshot, SkillRoot
from gc_core.settings import LoadedSettings, RuntimeEnv, runtime_of
from gc_core.tools import _REMOTE_OS_BACKENDS


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()


def _plugin(parent, name, *, skill=True):
    root = parent / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    if skill:
        (root / "skills" / "s").mkdir(parents=True)
        (root / "skills" / "s" / "SKILL.md").write_text(
            "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
        )
    return root


def _loaded(runtime=None, **sections):
    return LoadedSettings(
        settings=AppConfig.model_validate(sections),
        runtime=runtime or RuntimeEnv(),
    )


def _names(snap):
    return tuple(p.name for p in snap.plugins)


def test_snapshot_lists_installed_and_paths(tmp_path):
    local = _plugin(tmp_path, "local")
    installed.install(str(_plugin(tmp_path / "src", "inst")))

    snap = build_plugin_snapshot(
        _loaded(plugins={"paths": [str(local)]}), clone=False
    )

    assert _names(snap) == ("local", "inst")
    assert snap.trusted == frozenset({"local", "inst"})
    assert SkillRoot("local", str(local.resolve() / "skills"), None) in (
        snap.skill_roots
    )
    assert snap.mcp_servers == {}
    assert snap.roles == ()


@pytest.mark.parametrize("backend", sorted(_REMOTE_OS_BACKENDS))
def test_snapshot_empty_on_remote_backend(tmp_path, backend):
    local = _plugin(tmp_path, "local")
    loaded = _loaded(
        RuntimeEnv(os_backend=backend), plugins={"paths": [str(local)]}
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert snap.plugins == ()
    assert snap.skill_roots == ()
    assert len(snap.notices) == 1
    assert backend in snap.notices[0]


def test_snapshot_empty_with_custom_agent_store(tmp_path):
    local = _plugin(tmp_path, "local")
    loaded = _loaded(
        plugins={"paths": [str(local)]},
        storage={"agent": {"type": "postgres", "dsn": "x"}},
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert snap.plugins == ()
    assert "storage.agent" in snap.notices[0]


def test_snapshot_kept_with_db_threads_and_file_agent_store(tmp_path):
    local = _plugin(tmp_path, "local")
    loaded = _loaded(
        plugins={"paths": [str(local)]},
        storage={"threads": {"type": "postgres", "dsn": "x"}},
    )

    assert _names(build_plugin_snapshot(loaded, clone=False)) == ("local",)


def test_snapshot_empty_in_single_source_skills_mode(tmp_path):
    local = _plugin(tmp_path, "local")
    loaded = _loaded(
        plugins={"paths": [str(local)]},
        paths={"skills_data_path": str(tmp_path / "skills")},
    )

    snap = build_plugin_snapshot(loaded, clone=False)

    assert snap.plugins == ()
    assert "paths.skills_data_path" in snap.notices[0]


def test_disabled_without_plugins_has_no_notice():
    loaded = _loaded(RuntimeEnv(os_backend="docker"))

    assert build_plugin_snapshot(loaded, clone=False).notices == ()


def test_missing_source_without_clone_is_notice(tmp_path, monkeypatch):
    def boom(*_a, **_k):
        raise AssertionError("git without clone")

    monkeypatch.setattr(sources, "_clone", boom)
    loaded = _loaded(plugins={"sources": [{"git": str(tmp_path / "repo")}]})

    snap = build_plugin_snapshot(loaded, clone=False)

    assert snap.plugins == ()
    assert len(snap.notices) == 1
    assert str(tmp_path / "repo") in snap.notices[0]


def test_with_plugin_snapshot_appends_notices(tmp_path, monkeypatch):
    monkeypatch.setattr(sources, "_clone", lambda *a, **k: None)
    loaded = _loaded(
        RuntimeEnv(notices=("старое",)),
        plugins={"sources": [{"git": str(tmp_path / "repo")}]},
    )

    result = with_plugin_snapshot(loaded, clone=False)

    assert runtime_of(result).plugins == build_plugin_snapshot(
        loaded, clone=False
    )
    assert runtime_of(result).notices[0] == "старое"
    assert len(runtime_of(result).notices) == 2


# --- доверие (§7.1) -------------------------------------------------------


@pytest.fixture
def layout(tmp_path, monkeypatch):
    """База в ``home/config.yml``, проект — cwd с ``.gc/``."""
    home = tmp_path / "home"
    project = tmp_path / "project"
    (project / ".gc" / "profiles").mkdir(parents=True)
    home.mkdir()
    monkeypatch.chdir(project)
    return home, project


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def _paths_yaml(*paths):
    items = "".join(f"\n    - {p}" for p in paths)
    return f"plugins:\n  paths:{items}\n"


def _load(home):
    loaded = load_settings(str(home / "config.yml"))
    return loaded, build_plugin_snapshot(loaded, clone=False)


def test_project_layer_plugin_untrusted(layout, tmp_path):
    home, project = layout
    plug = _plugin(tmp_path, "proj")
    _write(home / "config.yml", "agent:\n  max_steps: 5\n")
    _write(project / ".gc" / "config.yml", _paths_yaml(plug))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == {str(plug)}
    assert _names(snap) == ("proj",)
    assert snap.trusted == frozenset()


def test_project_profile_plugin_untrusted(layout, tmp_path):
    home, project = layout
    plug = _plugin(tmp_path, "prof")
    _write(home / "config.yml", "profile: x\n")
    _write(project / ".gc" / "profiles" / "x.yml", _paths_yaml(plug))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == {str(plug)}
    assert _names(snap) == ("prof",)
    assert snap.trusted == frozenset()


def test_user_profile_plugin_trusted(layout, tmp_path):
    home, _project = layout
    plug = _plugin(tmp_path, "prof")
    _write(home / "config.yml", "profile: x\n")
    _write(home / "profiles" / "x.yml", _paths_yaml(plug))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == frozenset()
    assert snap.trusted == frozenset({"prof"})


def test_project_relative_path_detected_after_normalization(layout):
    home, project = layout
    plug = _plugin(home / "plugs", "rel")
    _write(home / "config.yml", "agent:\n  max_steps: 5\n")
    _write(project / ".gc" / "config.yml", _paths_yaml("plugs/rel"))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == {str(plug)}
    assert _names(snap) == ("rel",)
    assert snap.trusted == frozenset()


def test_project_source_untrusted(layout, tmp_path, monkeypatch):
    home, project = layout
    _plugin(tmp_path, "gitplug")
    repo = str(tmp_path / "gitplug")
    clone_dir = sources.cache_dir(repo, None)

    def fake_clone(_url, _ref, target):
        shutil.copytree(repo, target)
        (target / ".git").mkdir()

    monkeypatch.setattr(sources, "_clone", fake_clone)
    _write(home / "config.yml", "agent:\n  max_steps: 5\n")
    _write(
        project / ".gc" / "config.yml",
        f"plugins:\n  sources:\n    - git: {repo}\n",
    )

    loaded = load_settings(str(home / "config.yml"))
    snap = build_plugin_snapshot(loaded, clone=True)

    assert runtime_of(loaded).untrusted_plugin_entries == {str(clone_dir)}
    assert _names(snap) == ("gitplug",)
    assert snap.trusted == frozenset()


def test_user_config_and_installed_plugins_trusted(layout, tmp_path):
    home, _project = layout
    plug = _plugin(tmp_path, "user")
    installed.install(str(_plugin(tmp_path / "src", "inst")))
    _write(home / "config.yml", _paths_yaml(plug))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == frozenset()
    assert snap.trusted == frozenset({"user", "inst"})


def test_installed_plugin_listed_by_project_is_trusted(layout, tmp_path):
    home, project = layout
    installed.install(str(_plugin(tmp_path / "src", "inst")))
    _write(home / "config.yml", "agent:\n  max_steps: 5\n")
    _write(
        project / ".gc" / "config.yml",
        _paths_yaml(tmp_path / "inst" / "inst"),
    )

    _loaded_settings, snap = _load(home)

    assert snap.trusted == frozenset({"inst"})


def test_entry_in_both_project_and_user_is_trusted(layout, tmp_path):
    home, project = layout
    plug = _plugin(tmp_path, "both")
    _write(home / "config.yml", _paths_yaml(plug))
    _write(project / ".gc" / "config.yml", _paths_yaml(plug))

    loaded, snap = _load(home)

    assert runtime_of(loaded).untrusted_plugin_entries == frozenset()
    assert snap.trusted == frozenset({"both"})


def test_relative_plugin_path_resolved_from_base_config_dir(
    layout, monkeypatch
):
    home, _project = layout
    _write(home / "config.yml", _paths_yaml("plugs/a", "~/b"))
    monkeypatch.setenv("HOME", str(home))

    loaded = load_settings(str(home / "config.yml"))

    assert loaded.settings.plugins.paths == [
        str(home / "plugs" / "a"),
        str(home / "b"),
    ]


def test_fingerprint_changes_on_installed_json_and_plugin_dir(tmp_path):
    local = _plugin(tmp_path, "local")
    loaded = _loaded(plugins={"paths": [str(local)]})
    first = plugin_fingerprint(loaded)
    assert plugin_fingerprint(loaded) == first

    installed.install(str(_plugin(tmp_path / "src", "inst")))
    second = plugin_fingerprint(loaded)
    assert second != first

    replaced = tmp_path / "replaced"
    shutil.copytree(local, replaced)
    shutil.rmtree(local)
    os.rename(replaced, local)
    assert plugin_fingerprint(loaded) != second


def test_fingerprint_changes_with_mode(tmp_path):
    local = _plugin(tmp_path, "local")
    on = _loaded(plugins={"paths": [str(local)]})
    off = _loaded(
        RuntimeEnv(os_backend="ssh"), plugins={"paths": [str(local)]}
    )

    assert plugin_fingerprint(on) != plugin_fingerprint(off)


def test_snapshot_carries_fingerprint(tmp_path):
    loaded = _loaded(plugins={"paths": [str(_plugin(tmp_path, "local"))]})

    snap = build_plugin_snapshot(loaded, clone=False)

    assert snap.fingerprint == plugin_fingerprint(loaded)


def test_runtime_defaults_are_empty():
    assert RuntimeEnv().plugins == PluginSnapshot()
    assert RuntimeEnv().untrusted_plugin_entries == frozenset()
