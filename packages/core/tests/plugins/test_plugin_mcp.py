"""Серверы MCP плагинов — отдельный источник из снимка набора (§5.1)."""

import json
import subprocess
import sys

import pytest
from gc_core.config import load_settings
from gc_core.config_modules.mcp import McpServerConfig
from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.constants.tool_names import shorten_tool_name
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.catalog import build_plugin_snapshot
from gc_core.plugins.manifest import load_plugin
from gc_core.plugins.mcp_servers import plugin_mcp_part
from gc_core.settings import (
    LoadedSettings,
    default_settings,
    runtime_of,
    settings_of,
)
from gc_core.tools.connect_mcp.effective import effective_mcp_servers


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()


def _plugin(parent, name, servers):
    root = parent / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": servers}), encoding="utf-8"
    )
    return root


def _loaded(tmp_path, *roots, **sections):
    cfg = AppConfig.model_validate(
        {"plugins": {"paths": [str(r) for r in roots]}, **sections}
    )
    return LoadedSettings(settings=resolve_paths(cfg, str(tmp_path)))


def _with_snapshot(loaded):
    return loaded.with_runtime(
        plugins=build_plugin_snapshot(loaded, clone=False)
    )


HTTP = {"type": "http", "url": "https://e.x/mcp"}


def test_appconfig_has_no_plugin_servers(tmp_path):
    root = _plugin(tmp_path, "pf", {"srv": HTTP})
    (tmp_path / "config.yml").write_text(
        f"plugins:\n  paths:\n    - {root}\n", encoding="utf-8"
    )
    loaded = _with_snapshot(load_settings(str(tmp_path / "config.yml")))

    assert "pf_srv" not in settings_of(loaded).mcp.servers
    assert "pf_srv" not in default_settings().mcp.servers
    assert "pf_srv" in effective_mcp_servers(loaded)
    assert runtime_of(loaded).plugins.mcp_keys == {("pf", "srv"): "pf_srv"}


def test_resolve_paths_never_clones_or_reads_plugins(tmp_path, monkeypatch):
    calls = []
    monkeypatch.setattr(sources, "_clone", lambda *a: calls.append(a))
    monkeypatch.setattr(catalog, "load_plugin", lambda *a: calls.append(a))
    cfg = AppConfig.model_validate(
        {
            "plugins": {
                "paths": [str(tmp_path / "p")],
                "sources": [{"git": "https://e.x/repo.git"}],
            }
        }
    )

    resolve_paths(cfg, str(tmp_path))

    assert calls == []


_IMPORTS_PROBE = """
import sys
from gc_core.config_modules.root import AppConfig, resolve_paths
cfg = AppConfig.model_validate({"plugins": {"paths": ["/nonexistent"]}})
resolve_paths(cfg, "/")
assert not [m for m in sys.modules if m.startswith("gc_core.plugins")]
import gc_core.plugins.catalog
assert "gc_core.tools" not in sys.modules
"""


def test_config_load_imports_neither_plugins_nor_tools():
    done = subprocess.run(
        [sys.executable, "-c", _IMPORTS_PROBE], capture_output=True, text=True
    )
    assert done.returncode == 0, done.stderr


def test_override_mcp_servers_empty_keeps_plugin_servers(tmp_path):
    root = _plugin(tmp_path, "pf", {"srv": HTTP})
    loaded = _loaded(
        tmp_path,
        root,
        mcp={
            "servers": {
                "own": {"transport": "stream_http", "url": "https://o.x/"}
            }
        },
    ).with_overrides({"mcp.servers": {}})

    servers = effective_mcp_servers(_with_snapshot(loaded))

    assert list(servers) == ["pf_srv"]


def test_user_entry_replaces_plugin_server_whole_and_reported(tmp_path):
    root = _plugin(tmp_path, "pf", {"srv": {**HTTP, "description": "pl"}})
    loaded = _with_snapshot(
        _loaded(
            tmp_path,
            root,
            mcp={
                "servers": {
                    "pf_srv": {
                        "transport": "stream_http",
                        "url": "https://user.x/",
                    }
                }
            },
        )
    )
    snap = runtime_of(loaded).plugins

    server = effective_mcp_servers(loaded)["pf_srv"]

    assert server.url == "https://user.x/"
    assert server.description is None
    assert "pf_srv" not in snap.mcp_servers
    assert snap.mcp_keys == {("pf", "srv"): "pf_srv"}
    assert any("pf_srv" in n and "mcp.servers" in n for n in snap.notices)


def test_effective_user_entry_wins_over_stale_snapshot(tmp_path):
    root = _plugin(tmp_path, "pf", {"srv": HTTP})
    loaded = _with_snapshot(_loaded(tmp_path, root)).with_overrides(
        {
            "mcp.servers": {
                "pf_srv": {
                    "transport": "stream_http",
                    "url": "https://user.x/",
                }
            }
        }
    )

    assert effective_mcp_servers(loaded)["pf_srv"].url == "https://user.x/"


def test_key_collision_between_and_within_plugins(tmp_path):
    first = load_plugin(_plugin(tmp_path, "a_b", {"c": HTTP, "x.y": HTTP}))
    second = load_plugin(
        _plugin(tmp_path, "a", {"b_c": {"url": "https://other/"}, "x_y": HTTP})
    )
    within = load_plugin(_plugin(tmp_path, "w", {"s.t": HTTP, "s_t": HTTP}))

    servers, keys, notices = plugin_mcp_part((first, second, within), {})

    assert set(servers) == {"a_b_c", "a_b_x_y", "a_x_y", "w_s_t"}
    assert servers["a_b_c"].url == "https://e.x/mcp"
    assert keys == {
        ("a_b", "c"): "a_b_c",
        ("a_b", "x.y"): "a_b_x_y",
        ("a", "x_y"): "a_x_y",
        ("w", "s.t"): "w_s_t",
    }
    assert len(notices) == 2
    assert any("b_c" in n and "a_b_c" in n and "a_b" in n for n in notices)
    assert any("s_t" in n and "w_s_t" in n for n in notices)


def test_invalid_plugin_server_skipped_without_values_in_log(tmp_path, caplog):
    plugin = load_plugin(
        _plugin(
            tmp_path,
            "p",
            {
                "bad": {"command": "echo", "args": "SECRET123"},
                "good": {"command": "echo"},
            },
        )
    )

    with caplog.at_level("WARNING"):
        servers, keys, notices = plugin_mcp_part((plugin,), {})

    assert list(servers) == ["p_good"]
    assert isinstance(servers["p_good"], McpServerConfig)
    assert keys == {("p", "good"): "p_good"}
    assert len(notices) == 1 and "p_bad" in notices[0]
    assert "args" in notices[0]
    assert "p_bad" in caplog.text
    assert "SECRET123" not in caplog.text
    assert "SECRET123" not in notices[0]


def test_disabled_plugins_give_no_servers(tmp_path):
    root = _plugin(tmp_path, "pf", {"srv": HTTP})
    loaded = _loaded(
        tmp_path, root, paths={"skills_data_path": str(tmp_path / "s")}
    )

    assert effective_mcp_servers(_with_snapshot(loaded)) == {}


def test_shorten_tool_name_keeps_short_and_hashes_long():
    assert shorten_tool_name("mcp_a_b") == "mcp_a_b"
    long_a = shorten_tool_name("x" * 70 + "a")
    long_b = shorten_tool_name("x" * 70 + "b")
    assert len(long_a) == 64 and long_a != long_b
    assert long_a == shorten_tool_name("x" * 70 + "a")
    assert len(shorten_tool_name("y" * 50, limit=20)) == 20
