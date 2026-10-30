"""Опорный набор: офлайн-копии раскладки реальных плагинов, что видит агент."""

import json
import os
from types import SimpleNamespace

import pytest

from gc_core.config_modules.root import AppConfig, resolve_paths
from gc_core.config_modules.plugins import PluginsConfig
from gc_core.plugins import catalog, installed, runtime
from gc_core.settings import LoadedSettings
from gc_core.tools.skills.registry import SkillsRegistry
from gc_core.tools.subagents.custom.file_definitions import (
    adiscover_subagent_definitions,
)
from gc_testkit.settings import make_settings

AP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
SKILL = "---\nname: {name}\ndescription: d\n---\nbody\n"


@pytest.fixture(autouse=True)
def _isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")
    monkeypatch.setattr(installed, "enabled_paths", lambda root=None: [])
    catalog.reset_plugin_cache()


def _write(path, text):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _manifest(root, data):
    _write(root / ".claude-plugin" / "plugin.json", json.dumps(data))


def _skill(root, name):
    _write(root / "skills" / name / "SKILL.md", SKILL.format(name=name))


async def _skill_ids(settings, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir(exist_ok=True)
    reg = SkillsRegistry(
        system_root=str(empty),
        user_root=str(empty),
        project_root=str(empty),
        plugin_roots=runtime.plugin_skill_roots(settings),
        plugin_commands=runtime.plugin_command_defs(settings),
    )
    return {
        s["id"] if isinstance(s, dict) else s.id
        for s in await reg.list_skills()
    }


def _settings(root):
    return make_settings(
        {"plugins.paths": [str(root)], "plugins.use_installed": False}
    )


async def test_superpowers_skills_and_session_start_once(
    tmp_path, monkeypatch
):
    root = tmp_path / "superpowers"
    _manifest(root, {"name": "superpowers", "version": "7.0.1"})
    _skill(root, "brainstorming")
    _skill(root, "using-superpowers")
    script = _write(
        root / "hooks" / "run-hook.cmd",
        "#!/usr/bin/env bash\n"
        'echo \'{"hookSpecificOutput": {"additionalContext": '
        '"You have superpowers."}}\'\n',
    )
    os.chmod(script, 0o755)
    hooks = {
        "SessionStart": [
            {
                "matcher": "startup|clear|compact",
                "hooks": [
                    {
                        "type": "command",
                        "command": '"${CLAUDE_PLUGIN_ROOT}/hooks/run-hook.cmd"'
                        " session-start",
                    }
                ],
            }
        ]
    }
    _write(root / "hooks" / "hooks.json", json.dumps({"hooks": hooks}))

    settings = _settings(root)
    assert {
        "superpowers:brainstorming",
        "superpowers:using-superpowers",
    } <= await _skill_ids(settings, tmp_path)

    monkeypatch.setattr(
        runtime,
        "_plugins_config",
        lambda ctx: PluginsConfig(paths=[str(root)], hook_timeout_sec=10),
    )
    ctx = SimpleNamespace()
    state = SimpleNamespace(state_id="s1", messages=[])
    first, started = await runtime.asession_start_messages(state, ctx)
    assert started and len(first) == 1
    assert "You have superpowers." in first[0]["content"]
    # история треда уже содержит контекст: повторно хук не идёт
    again = SimpleNamespace(state_id="s2", messages=first)
    assert await runtime.asession_start_messages(again, ctx) == ([], True)


async def test_agent_plugins_example_format_and_skill(tmp_path):
    root = tmp_path / "agent-plugins-example"
    _write(
        root / "plugin.json",
        json.dumps(
            {
                "$schema": AP_SCHEMA,
                "name": "agent-plugins-example",
                "version": "1.0.0",
                "license": "MIT",
                "keywords": ["example", "migration"],
            }
        ),
    )
    _skill(root, "migrate-agent-plugin")
    settings = _settings(root)
    (plugin,) = catalog.load_plugins([str(root)])
    assert plugin.format == "agent-plugins"
    assert "agent-plugins-example:migrate-agent-plugin" in await _skill_ids(
        settings, tmp_path
    )


def test_playwright_mcp_is_stdio_npx(tmp_path):
    root = tmp_path / "playwright"
    _manifest(root, {"name": "playwright"})
    _write(
        root / ".mcp.json",
        json.dumps(
            {
                "playwright": {
                    "command": "npx",
                    "args": ["@playwright/mcp@latest"],
                }
            }
        ),
    )
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    loaded = LoadedSettings(settings=resolve_paths(cfg, str(tmp_path)))
    snap = catalog.build_plugin_snapshot(loaded, clone=False)
    server = snap.mcp_servers["playwright"]
    assert server.transport == "stdio"
    assert server.command == "npx"
    assert server.args == ["@playwright/mcp@latest"]


async def test_backend_development_agent_command_skill(tmp_path):
    root = tmp_path / "backend-development"
    _manifest(root, {"name": "backend-development", "version": "1.0.0"})
    _write(
        root / ".codex-plugin" / "plugin.json",
        json.dumps({"name": "backend-development"}),
    )
    _write(
        root / "agents" / "backend-architect.md",
        "---\nname: backend-architect\ndescription: Designs backends.\n"
        "model: inherit\n---\nYou are a backend architect.\n",
    )
    _write(
        root / "commands" / "feature-development.md",
        "---\ndescription: Build a feature\n---\nImplement: $ARGUMENTS\n",
    )
    _skill(root, "api-design-principles")

    settings = _settings(root)
    ids = await _skill_ids(settings, tmp_path)
    assert "backend-development:feature-development" in ids
    assert "backend-development:api-design-principles" in ids

    definitions = await adiscover_subagent_definitions(
        make_settings(
            {
                "plugins.paths": [str(root)],
                "plugins.use_installed": False,
                "agent.use_local_subagents": True,
            }
        ),
        builtin_names=set(),
    )
    assert [d.name for d in definitions] == [
        "subagent_backend_development_backend_architect"
    ]
