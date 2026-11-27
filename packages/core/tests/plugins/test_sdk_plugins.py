"""Плагины в SDK: ``AsyncAgent.create(plugins=[...])``.

Набор агента — ровно переданные каталоги: установленные и плагины конфига
вызывающего не подхватываются, переданные доверены (их передал сам
разработчик), ограничения первой версии — как у хостов.
"""

import asyncio
import dataclasses
import json

import pytest
from gc_core.aio import AsyncAgent
from gc_core.config_modules.root import AppConfig
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.catalog import with_plugin_snapshot
from gc_core.plugins.manifest import load_plugin
from gc_core.settings import LoadedSettings, RuntimeEnv
from gc_core.tools import _REMOTE_OS_BACKENDS
from parity_harness import parity_config


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()


def _plugin(parent, name):
    """Каталог плагина со skill, командой и хуком."""
    root = parent / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "skills" / "s").mkdir(parents=True)
    (root / "skills" / "s" / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (root / "commands").mkdir()
    (root / "commands" / "go.md").write_text("Сделай $1", encoding="utf-8")
    (root / "hooks").mkdir()
    hook = {"hooks": [{"type": "command", "command": "true"}]}
    (root / "hooks" / "hooks.json").write_text(
        json.dumps({"hooks": {"SessionStart": [hook]}}), encoding="utf-8"
    )
    return root


async def _ctx_of(settings, **kwargs):
    agent = await AsyncAgent.create(settings, **kwargs)
    ctx = agent.executor._ctx
    skills = await ctx.skills_registry.list_skills()
    await agent.aclose()
    ids = {s["id"] if isinstance(s, dict) else s.id for s in skills}
    return ctx, ids


def _names(snap):
    return tuple(p.name for p in snap.plugins)


async def test_plugin_path_gives_snapshot_skill_and_command(
    tmp_path, monkeypatch
):
    root = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(tmp_path, monkeypatch)

    ctx, skill_ids = await _ctx_of(cfg, plugins=[root])

    snap = ctx.runtime.plugins
    assert _names(snap) == ("alpha",)
    assert "alpha:s" in skill_ids
    assert [(p, c.name) for p, c in snap.commands] == [("alpha", "go")]


async def test_loaded_plugin_object_is_accepted(tmp_path, monkeypatch):
    plugin = load_plugin(_plugin(tmp_path / "plugs", "alpha"))
    cfg = parity_config(tmp_path, monkeypatch)

    ctx, skill_ids = await _ctx_of(cfg, plugins=[plugin])

    assert _names(ctx.runtime.plugins) == ("alpha",)
    assert "alpha:s" in skill_ids


async def test_app_config_settings_accepted(tmp_path, monkeypatch):
    root = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(tmp_path, monkeypatch).settings
    assert isinstance(cfg, AppConfig)

    ctx, _ = await _ctx_of(cfg, plugins=[str(root)])

    assert _names(ctx.runtime.plugins) == ("alpha",)


async def test_installed_and_config_plugins_not_picked(tmp_path, monkeypatch):
    installed.install(str(_plugin(tmp_path / "src", "inst")))
    from_config = _plugin(tmp_path / "plugs", "conf")
    passed = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(
        tmp_path, monkeypatch, **{"plugins.paths": [str(from_config)]}
    )

    ctx, skill_ids = await _ctx_of(cfg, plugins=[passed])

    assert _names(ctx.runtime.plugins) == ("alpha",)
    assert "inst:s" not in skill_ids
    assert "conf:s" not in skill_ids


async def test_without_parameter_behaviour_unchanged(tmp_path, monkeypatch):
    installed.install(str(_plugin(tmp_path / "src", "inst")))
    cfg = parity_config(tmp_path, monkeypatch)

    bare, _ = await _ctx_of(cfg)
    hosted, _ = await _ctx_of(with_plugin_snapshot(cfg, clone=False))

    assert bare.runtime.plugins.plugins == ()
    assert _names(hosted.runtime.plugins) == ("inst",)


async def test_empty_list_means_no_plugins(tmp_path, monkeypatch):
    installed.install(str(_plugin(tmp_path / "src", "inst")))
    cfg = with_plugin_snapshot(parity_config(tmp_path, monkeypatch))

    ctx, skill_ids = await _ctx_of(cfg, plugins=[])

    assert ctx.runtime.plugins.plugins == ()
    assert "inst:s" not in skill_ids


async def test_passed_plugin_trusted_even_if_listed_by_project(
    tmp_path, monkeypatch
):
    root = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(tmp_path, monkeypatch).with_runtime(
        untrusted_plugin_entries=frozenset({str(root)})
    )

    ctx, _ = await _ctx_of(cfg, plugins=[root])

    assert ctx.runtime.plugins.trusted == frozenset({"alpha"})


async def test_order_kept_and_broken_path_skipped(tmp_path, monkeypatch):
    first = _plugin(tmp_path / "plugs", "zeta")
    second = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(tmp_path, monkeypatch)

    ctx, _ = await _ctx_of(cfg, plugins=[first, tmp_path / "nope", second])

    assert _names(ctx.runtime.plugins) == ("zeta", "alpha")


async def test_relative_path_resolved_from_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    _plugin(work, "alpha")
    cfg = parity_config(tmp_path / "conf", monkeypatch)
    monkeypatch.chdir(work)

    ctx, _ = await _ctx_of(cfg, plugins=["alpha"])

    assert _names(ctx.runtime.plugins) == ("alpha",)


async def test_home_tilde_expanded(tmp_path, monkeypatch):
    home = tmp_path / "home"
    _plugin(home, "alpha")
    cfg = parity_config(tmp_path, monkeypatch)
    monkeypatch.setenv("HOME", str(home))

    ctx, _ = await _ctx_of(cfg, plugins=["~/alpha"])

    assert _names(ctx.runtime.plugins) == ("alpha",)


@pytest.mark.parametrize("single", ["plug", b"plug", "path"])
async def test_single_path_value_rejected(tmp_path, monkeypatch, single):
    root = _plugin(tmp_path / "plugs", "plug")
    value = root if single == "path" else single
    cfg = parity_config(tmp_path, monkeypatch)

    with pytest.raises(TypeError, match="списком"):
        await AsyncAgent.create(cfg, plugins=value)


async def test_explicit_runtime_keeps_plugins(tmp_path, monkeypatch):
    root = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(tmp_path, monkeypatch)
    runtime = dataclasses.replace(cfg.runtime, external_user_words=True)

    ctx, _ = await _ctx_of(cfg, plugins=[root], runtime=runtime)

    assert ctx.runtime.external_user_words is True
    assert _names(ctx.runtime.plugins) == ("alpha",)


async def test_single_source_skills_mode_empties_set(tmp_path, monkeypatch):
    root = _plugin(tmp_path / "plugs", "alpha")
    cfg = parity_config(
        tmp_path,
        monkeypatch,
        **{"paths.skills_data_path": str(tmp_path / "skills")},
    )

    ctx, _ = await _ctx_of(cfg, plugins=[root])

    assert ctx.runtime.plugins.plugins == ()
    assert any("paths.skills_data_path" in n for n in ctx.runtime.notices)


def test_durable_rejects_plugins(tmp_path):
    class _Durable:
        async def open(self, settings):  # pragma: no cover - не дойдёт
            raise AssertionError("open не должен вызываться")

    with pytest.raises(TypeError, match="plugins и параметры"):
        asyncio.run(
            AsyncAgent.create(
                AppConfig(), durable=_Durable(), plugins=[tmp_path]
            )
        )


@pytest.mark.parametrize("backend", sorted(_REMOTE_OS_BACKENDS))
def test_remote_backend_empties_set(tmp_path, backend):
    root = _plugin(tmp_path / "plugs", "alpha")
    loaded = LoadedSettings(
        settings=AppConfig(), runtime=RuntimeEnv(os_backend=backend)
    )

    result = catalog.with_explicit_plugins(loaded, [root])

    assert result.runtime.plugins.plugins == ()
    assert any(backend in n for n in result.runtime.notices)


def test_non_file_agent_store_empties_set(tmp_path):
    root = _plugin(tmp_path / "plugs", "alpha")
    loaded = LoadedSettings(
        settings=AppConfig.model_validate(
            {"storage": {"agent": {"type": "postgres", "dsn": "x"}}}
        )
    )

    result = catalog.with_explicit_plugins(loaded, [root])

    assert result.runtime.plugins.plugins == ()
    assert any("storage.agent" in n for n in result.runtime.notices)
