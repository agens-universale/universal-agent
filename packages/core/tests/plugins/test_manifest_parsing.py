"""Разбор манифеста: инструменты ролей и пути компонентов."""

import json

import pytest
from gc_core.plugins.manifest import load_plugin


def _plugin(tmp_path, manifest=None):
    root = tmp_path / "p"
    tmp_path.mkdir(parents=True, exist_ok=True)
    (root / ".claude-plugin").mkdir(parents=True)
    data = {"name": "m", **(manifest or {})}
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps(data), encoding="utf-8"
    )
    return root


def load_agent_raw(tmp_path, text):
    root = _plugin(tmp_path)
    (root / "agents").mkdir()
    (root / "agents" / "role.md").write_text(text, encoding="utf-8")
    agents = load_plugin(root).agents
    return agents[0] if agents else None


def load_agent(tmp_path, header):
    head = f"---\n{header}\n---\n" if header else ""
    return load_agent_raw(tmp_path, head + "Текст роли")


@pytest.mark.parametrize(
    "header,expected",
    [
        ("", None),
        ("description: x", None),
        ("tools: []", ()),
        ('tools: ""', ()),
        ("tools:", ()),
        ("tools: {}", ()),
        ("tools: 5", ()),
        ("tools: Read, Bash", ("Read", "Bash")),
    ],
)
def test_agent_tools_key_present_never_means_all(tmp_path, header, expected):
    assert load_agent(tmp_path, header).tools == expected


def test_wrong_type_and_null_report_problem(tmp_path):
    for i, (header, has_problem) in enumerate(
        [("tools:", True), ("tools: 5", True), ("tools: []", False)]
    ):
        agent = load_agent(tmp_path / str(i), header)
        assert bool(agent.problem) is has_problem


@pytest.mark.parametrize(
    "text",
    [
        "---\ntools: [Read\n---\nbody",
        "---\ntools: Read\nbody without closing",
        "---\n- a\n- b\n---\nbody",
    ],
)
def test_broken_header_role_never_gets_all_tools(tmp_path, text):
    agent = load_agent_raw(tmp_path, text)
    assert agent is None or agent.tools == ()
    assert agent is None or agent.problem


def test_agent_without_header_keeps_all_tools(tmp_path):
    agent = load_agent_raw(tmp_path, "Просто текст роли")
    assert (agent.tools, agent.problem) == (None, "")


def test_manifest_declared_skills_paths_extend_standard(tmp_path):
    root = _plugin(tmp_path, {"skills": ["./extra"]})
    (root / "skills").mkdir()
    (root / "extra").mkdir()
    plugin = load_plugin(root)
    assert [d.name for d in plugin.skills_dirs] == ["skills", "extra"]


def test_manifest_skills_path_duplicate_counted_once(tmp_path):
    root = _plugin(tmp_path, {"skills": "./skills"})
    (root / "skills").mkdir()
    assert len(load_plugin(root).skills_dirs) == 1


def test_manifest_path_outside_plugin_dropped(tmp_path):
    root = _plugin(tmp_path, {"skills": ["../outside", "./in"]})
    (tmp_path / "outside").mkdir()
    (root / "in").mkdir()
    plugin = load_plugin(root)
    assert [d.name for d in plugin.skills_dirs] == ["in"]


def test_manifest_symlink_out_of_plugin_dropped(tmp_path):
    root = _plugin(tmp_path, {"skills": ["./link"]})
    (tmp_path / "outside").mkdir()
    (root / "link").symlink_to(tmp_path / "outside")
    assert load_plugin(root).skills_dirs == ()


def test_manifest_declared_commands_agents_mcp(tmp_path):
    root = _plugin(
        tmp_path,
        {
            "commands": "./cmds",
            "agents": ["./team/role.md"],
            "mcpServers": {"inline": {"command": "srv"}},
        },
    )
    (root / "cmds").mkdir()
    (root / "cmds" / "go.md").write_text("Сделай", encoding="utf-8")
    (root / "team").mkdir()
    (root / "team" / "role.md").write_text("Роль", encoding="utf-8")
    plugin = load_plugin(root)
    assert [c.name for c in plugin.commands] == ["go"]
    assert [a.name for a in plugin.agents] == ["role"]
    assert list(plugin.mcp_servers) == ["inline"]


def test_manifest_mcp_servers_path_extends_standard(tmp_path):
    root = _plugin(tmp_path, {"mcpServers": "./extra.json"})
    (root / ".mcp.json").write_text(
        json.dumps({"a": {"command": "x"}}), encoding="utf-8"
    )
    (root / "extra.json").write_text(
        json.dumps({"mcpServers": {"b": {"command": "y"}}}),
        encoding="utf-8",
    )
    assert list(load_plugin(root).mcp_servers) == ["a", "b"]
