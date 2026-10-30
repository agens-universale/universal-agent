"""Сервер: снимок набора плагинов — в настройках поверхности, после
``agent_overrides``, один на поверхность за процесс (§8.3, §9)."""

import json
import shutil

from gc_core.plugins import sources
from gc_core.settings import runtime_of
from gc_server.app import ServerRuntime
from gc_server.cli import prewarm_plugin_sources
from gc_server.config import ServerConfig
from gc_testkit.plugin_store import install_plugin
from gc_testkit.settings import make_settings


def _runtime(base=None, **server):
    return ServerRuntime(
        base=make_settings(base or {}),
        config=ServerConfig.model_validate(server),
    )


def _names(loaded):
    return tuple(p.name for p in runtime_of(loaded).plugins.plugins)


def test_server_surface_lists_installed(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)

    assert _names(_runtime().surface_settings("chat")) == ("inst",)


def test_server_agent_overrides_use_installed_false(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    runtime = _runtime(agent_overrides={"plugins": {"use_installed": False}})

    assert _names(runtime.surface_settings("chat")) == ()


def test_surface_override_use_installed_false(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    runtime = _runtime(
        webhook={"agent_overrides": {"plugins.use_installed": False}}
    )

    assert _names(runtime.surface_settings("webhook")) == ()
    assert _names(runtime.surface_settings("chat")) == ("inst",)


def test_surface_snapshot_is_built_once(tmp_path, monkeypatch):
    install_plugin(tmp_path, monkeypatch)
    runtime = _runtime()

    first = runtime.surface_settings("chat")

    assert runtime.surface_settings("chat") is first


def _git_source(tmp_path, monkeypatch):
    repo = tmp_path / "gitplug"
    (repo / ".claude-plugin").mkdir(parents=True)
    (repo / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": "gitplug", "version": "1.0.0"}), encoding="utf-8"
    )
    clones = []

    def fake_clone(_url, _ref, target):
        clones.append(target)
        shutil.copytree(repo, target)
        (target / ".git").mkdir()

    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(sources, "_resolved", {})
    monkeypatch.setattr(sources, "_clone", fake_clone)
    return {"plugins.sources": [{"git": str(repo)}]}, clones


def test_prewarm_clones_sources_before_the_loop(tmp_path, monkeypatch):
    base, clones = _git_source(tmp_path, monkeypatch)
    runtime = _runtime(base)

    prewarm_plugin_sources(runtime.base, runtime.config, ["chat", "webhook"])

    assert len(clones) == 1
    assert _names(runtime.surface_settings("chat")) == ("gitplug",)
    assert len(clones) == 1


def test_surface_without_prewarm_does_not_clone(tmp_path, monkeypatch):
    base, clones = _git_source(tmp_path, monkeypatch)

    loaded = _runtime(base).surface_settings("chat")

    assert clones == []
    assert _names(loaded) == ()
    assert any("не подключён" in n for n in runtime_of(loaded).notices)


def test_prewarm_reports_clone_failure(tmp_path, monkeypatch, capsys):
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(sources, "_resolved", {})
    base = {"plugins.sources": [{"git": str(tmp_path / "nope")}]}
    runtime = _runtime(base)

    prewarm_plugin_sources(runtime.base, runtime.config, ["chat"])

    out = capsys.readouterr()
    assert "не подключён" in out.out + out.err
