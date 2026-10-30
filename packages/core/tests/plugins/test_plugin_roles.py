"""Роли плагинов: правило инструментов, перевод имён, MCP ребёнка (§6)."""

import inspect
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.config_modules.root import AppConfig
from gc_core.plugins import catalog, installed, sources
from gc_core.plugins.agents import TOOL_ALIASES, translate_role
from gc_core.plugins.catalog import with_plugin_snapshot
from gc_core.plugins.manifest import Plugin, PluginAgent
from gc_core.settings import LoadedSettings, RuntimeEnv, runtime_of
from gc_core.tools import apply_subagent_tools_filter
from gc_core.tools.connect_mcp.effective import effective_mcp_servers
from gc_core.tools.registry import all_builtin_tool_names
from gc_core.tools.subagents.custom import file_definitions
from gc_core.tools.subagents.custom.file_definitions import (
    adiscover_subagent_definitions,
    parse_subagent_file,
)
from gc_core.tools.subagents.custom.file_subagent import REQUIRED_TOOL_NAMES
from gc_core.tools.subagents.runner import SubagentRunner

BUILTIN = {"fs_read_file", "fs_list_files", "exec_command"}
KEYS = {("p", "gh"): "p_gh"}


@pytest.fixture(autouse=True)
def _isolated_store(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    catalog.reset_plugin_cache()


def plug(name="p"):
    return Plugin(name=name, version="1", root=Path("/x"), skills_dirs=())


def agent(tools, **fields):
    values = {
        "name": "r",
        "description": "d",
        "tools": tools,
        "body": "Роль.",
        "source": "/x/agents/r.md",
        **fields,
    }
    return PluginAgent(**values)


def translate(tools, keys=KEYS, **fields):
    return translate_role(
        plug(), agent(tools, **fields), builtin=BUILTIN, mcp_keys=keys
    )


@pytest.mark.parametrize(
    "tools,expected,connected",
    [
        (None, None, True),
        ((), (), True),
        (("Read", "WebSearch"), ("fs_read_file",), True),
        (("WebSearch",), None, False),
    ],
)
def test_tools_rule(tools, expected, connected):
    d, _ = translate(tools)
    assert (d is not None) is connected
    if connected:
        assert d.tools == expected
        assert d.name == "subagent_p_r"


def test_dropped_tools_reported_with_role_name():
    _, notices = translate(("Read", "WebSearch"))
    assert any("p:r" in n and "WebSearch" in n for n in notices)


def test_nothing_translated_reported():
    _, notices = translate(("WebSearch",))
    assert any("p:r" in n and "не подключена" in n for n in notices)


def test_null_or_wrong_type_gives_service_tools_and_notice():
    problem = "tools: ожидались строка или список, получено int"
    d, notices = translate((), problem=problem)
    assert d.tools == () and d.mcp_allow == ()
    assert any(problem in n and "p:r" in n for n in notices)


def test_broken_header_role_not_connected():
    d, notices = translate(
        (), problem="шапка не разобрана как YAML", header_broken=True
    )
    assert d is None
    assert any("шапка не разобрана как YAML" in n for n in notices)


@pytest.mark.parametrize(
    "name", ["mcp__gh__create", "mcp__plugin_p_gh__create"]
)
def test_mcp_tool_forms(name):
    d, _ = translate_role(
        plug("p"), agent((name,)), builtin=set(), mcp_keys=KEYS
    )
    assert d.mcp_allow == (("p_gh", "create"),)
    assert d.tools == ()


@pytest.mark.parametrize("name", ["mcp__gh", "mcp__gh__*"])
def test_whole_server_forms(name):
    d, _ = translate_role(
        plug("p"), agent((name,)), builtin=set(), mcp_keys=KEYS
    )
    assert d.mcp_allow == (("p_gh", None),)


def test_server_of_another_plugin_dropped():
    d, notices = translate_role(
        plug("p"),
        agent(("Read", "mcp__gh__create")),
        builtin=BUILTIN,
        mcp_keys={("q", "gh"): "q_gh"},
    )
    assert d.mcp_allow == ()
    assert any("mcp__gh__create" in n for n in notices)


def test_snake_case_unknown_dropped():
    d, notices = translate(("fs_list_files", "no_such_tool"))
    assert d.tools == ("fs_list_files",)
    assert any("no_such_tool" in n for n in notices)


def test_model_field_reported():
    d, notices = translate(("Read",), model="sonnet")
    assert d is not None
    assert any("p:r" in n and "model" in n for n in notices)


def test_model_inherit_not_reported():
    _, notices = translate(("Read",), model="inherit")
    assert notices == ()


def test_alias_table_targets_are_builtin_tools():
    names = all_builtin_tool_names(AppConfig())
    assert set(TOOL_ALIASES.values()) <= names


def _mcp(server, tool):
    return {
        "spec": {"name": f"mcp_{server}_{tool}"},
        "mcp_server": server,
        "mcp_tool": tool,
    }


def _data():
    data = {name: {"spec": {"name": name}} for name in REQUIRED_TOOL_NAMES}
    data["fs_read_file"] = {"spec": {"name": "fs_read_file"}}
    data["exec_command"] = {"spec": {"name": "exec_command"}}
    data["mcp_p_search"] = _mcp("p", "search")
    data["mcp_p_x_search"] = _mcp("p_x", "search")
    data["mcp_p_y_get"] = _mcp("p_y", "get")
    return data


def _scoped(allow, mcp_allow):
    return LoadedSettings(
        settings=AppConfig(),
        runtime=RuntimeEnv(
            subagent_tools_allow=allow, subagent_mcp_allow=mcp_allow
        ),
    )


def test_filter_does_not_capture_neighbour_server():
    out = apply_subagent_tools_filter(_data(), _scoped((), (("p", None),)))
    assert set(out) == {"mcp_p_search", *REQUIRED_TOOL_NAMES}
    out = apply_subagent_tools_filter(_data(), _scoped((), (("p", "search"),)))
    assert set(out) == {"mcp_p_search", *REQUIRED_TOOL_NAMES}


def test_only_mcp_role_keeps_its_mcp_tools():
    out = apply_subagent_tools_filter(
        _data(), _scoped((), (("p_x", "search"),))
    )
    assert set(out) == {"mcp_p_x_search", *REQUIRED_TOOL_NAMES}


def test_filter_accepts_pairs_restored_as_lists():
    out = apply_subagent_tools_filter(
        _data(), _scoped(("fs_read_file",), (["p", None],))
    )
    assert set(out) == {"fs_read_file", "mcp_p_search", *REQUIRED_TOOL_NAMES}


def test_tools_filter_unchanged_for_subagents_dir():
    d = parse_subagent_file(
        "/a/subagent_x.md",
        "---\nname: subagent_x\ndescription: d\n"
        "tools: [fs_read_file, mcp_p_search]\n---\nРоль.",
    )
    assert d.mcp_allow == ()
    out = apply_subagent_tools_filter(_data(), _scoped(d.tools, ()))
    assert set(out) == {"fs_read_file", "mcp_p_search", *REQUIRED_TOOL_NAMES}


# --- снимок и поиск определений -------------------------------------------


def _plugin_dir(parent, name="p", role=None, servers=None):
    root = parent / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.0.0"}), encoding="utf-8"
    )
    (root / "agents").mkdir()
    (root / "agents" / "r.md").write_text(
        role or "---\nname: r\ndescription: d\ntools: Read\n---\nРоль.",
        encoding="utf-8",
    )
    if servers is not None:
        (root / ".mcp.json").write_text(
            json.dumps({"mcpServers": servers}), encoding="utf-8"
        )
    skill = root / "skills" / "s"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text(
        "---\nname: s\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    return root


def _loaded(*roots, **sections):
    return LoadedSettings(
        settings=AppConfig.model_validate(
            {"plugins": {"paths": [str(r) for r in roots]}, **sections}
        )
    )


def test_snapshot_carries_translated_roles_and_notices(tmp_path):
    role = "---\nname: r\ndescription: d\ntools: Read, Task\nmodel: x\n---\nР."
    root = _plugin_dir(tmp_path, role=role)
    snap = runtime_of(with_plugin_snapshot(_loaded(root), clone=False))
    (d,) = snap.plugins.roles
    assert d.name == "subagent_p_r" and d.tools == ("fs_read_file",)
    assert any("Task" in n for n in snap.plugins.notices)
    assert any("model" in n for n in snap.notices)


async def test_discovery_reads_roles_from_snapshot_only(tmp_path):
    root = _plugin_dir(tmp_path)
    loaded = _loaded(root)
    assert await adiscover_subagent_definitions(loaded, builtin_names=()) == []
    found = await adiscover_subagent_definitions(
        with_plugin_snapshot(loaded, clone=False), builtin_names=()
    )
    assert [d.name for d in found] == ["subagent_p_r"]


async def test_discovery_without_settings_has_no_plugin_roles(
    tmp_path, monkeypatch
):
    plugin_root = _plugin_dir(tmp_path)
    monkeypatch.setattr(
        installed, "enabled_paths", lambda root=None: [str(plugin_root)]
    )
    assert await adiscover_subagent_definitions(None, builtin_names=()) == []


def test_file_definitions_does_not_import_plugins():
    assert "gc_core.plugins" not in inspect.getsource(file_definitions)


def test_child_sees_plugin_mcp_and_skills(tmp_path):
    role = (
        "---\nname: r\ndescription: d\n"
        "tools: [Read, mcp__gh__create]\n---\nРоль."
    )
    servers = {"gh": {"type": "http", "url": "https://e.x/mcp"}}
    root = _plugin_dir(tmp_path, role=role, servers=servers)
    parent = with_plugin_snapshot(
        _loaded(root, agent={"use_local_subagents": True}), clone=False
    )
    runner = SubagentRunner(
        SimpleNamespace(), SimpleNamespace(), settings=parent
    )
    runner._active_tool_name = "subagent_p_r"

    child = runner._build_child_config()

    rt = runtime_of(child)
    assert rt.subagent_tools_allow == ("fs_read_file",)
    assert rt.subagent_mcp_allow == (("p_gh", "create"),)
    assert rt.plugins.skill_roots == runtime_of(parent).plugins.skill_roots
    assert "p_gh" in effective_mcp_servers(child)
    data = {
        "mcp_p_gh_create": _mcp("p_gh", "create"),
        "mcp_p_gh_delete": _mcp("p_gh", "delete"),
        "exec_command": {"spec": {"name": "exec_command"}},
    }
    assert set(apply_subagent_tools_filter(data, child)) == {"mcp_p_gh_create"}
