"""Песочница плагинов: манифест, запуск хуков, фасад для цикла и skills."""

import json
import sys

import pytest
from types import SimpleNamespace

from gc_core.plugins import installed, runtime
from gc_core.plugins.hook_runner import run_hook
from gc_core.plugins.manifest import HookCommand, load_plugin
from gc_core.tools.skills.registry import SkillsRegistry

PY = sys.executable


@pytest.fixture(autouse=True)
def _isolated_installed_dir(tmp_path, monkeypatch):
    from gc_core.plugins import installed

    monkeypatch.setattr(installed, "installed_dir", lambda: tmp_path / "inst")


def _make_plugin(tmp_path, hooks=None, with_skill=False, name="demo"):
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.2.3"}), encoding="utf-8"
    )
    if hooks is not None:
        (root / "hooks").mkdir()
        (root / "hooks" / "hooks.json").write_text(
            json.dumps({"hooks": hooks}), encoding="utf-8"
        )
    if with_skill:
        skill = root / "skills" / "demo-skill"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text(
            "---\nname: demo-skill\ndescription: d\n---\nbody\n",
            encoding="utf-8",
        )
    return root


def _cmd(code: str) -> str:
    return f"{PY} -c {json.dumps(code)}"


def _config(monkeypatch, paths, timeout=10):
    runtime.reset_plugin_cache()
    # установленные у пользователя плагины в тесты не попадают
    monkeypatch.setattr(installed, "enabled_paths", lambda root=None: [])
    monkeypatch.setattr(
        runtime,
        "_plugins_config",
        lambda ctx: SimpleNamespace(paths=paths, hook_timeout_sec=timeout),
    )


def test_load_plugin_reads_manifest_hooks_and_skills(tmp_path):
    root = _make_plugin(
        tmp_path,
        hooks={
            "PreToolUse": [
                {
                    "matcher": "exec_command",
                    "hooks": [{"type": "command", "command": "true"}],
                }
            ],
            "Unknown": [{"hooks": [{"type": "command", "command": "true"}]}],
        },
        with_skill=True,
    )
    plugin = load_plugin(root)
    assert plugin is not None
    assert (plugin.name, plugin.version) == ("demo", "1.2.3")
    assert plugin.skills_dir == root / "skills"
    assert set(plugin.hooks) == {"PreToolUse"}
    assert plugin.hooks_for("PreToolUse", "exec_command")
    assert not plugin.hooks_for("PreToolUse", "fs_read_file")


def test_load_plugin_missing_dir_is_none(tmp_path):
    assert load_plugin(tmp_path / "nope") is None


def test_matcher_wildcard_and_alternatives(tmp_path):
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {
                    "matcher": "startup|clear|compact",
                    "hooks": [{"type": "command", "command": "true"}],
                }
            ],
            "PostToolUse": [
                {
                    "matcher": "*",
                    "hooks": [{"type": "command", "command": "true"}],
                }
            ],
        },
    )
    plugin = load_plugin(root)
    assert plugin.hooks_for("SessionStart", "startup")
    assert not plugin.hooks_for("SessionStart", "resume")
    assert plugin.hooks_for("PostToolUse", "any_tool")


async def test_hook_exit_code_2_blocks_with_stderr_reason(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    hook = HookCommand(
        command=_cmd("import sys; sys.stderr.write('nope'); sys.exit(2)")
    )
    res = await run_hook(
        plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.blocked and res.reason == "nope"


async def test_hook_json_deny_and_both_context_formats(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    deny = HookCommand(
        command=_cmd('print(\'{"decision": "deny", "reason": "r"}\')')
    )
    res = await run_hook(
        plugin, deny, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.blocked and res.reason == "r"

    nested = HookCommand(
        command=_cmd(
            'print(\'{"hookSpecificOutput": {"additionalContext": "A"}}\')'
        )
    )
    flat = HookCommand(command=_cmd('print(\'{"additionalContext": "B"}\')'))
    for hook, expected in ((nested, "A"), (flat, "B")):
        res = await run_hook(
            plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
        )
        assert res.additional_context == expected


async def test_hook_gets_env_and_stdin(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    hook = HookCommand(
        command=_cmd(
            "import json, os, sys; e = json.load(sys.stdin);"
            "print(json.dumps({'additionalContext':"
            " os.environ['CLAUDE_PLUGIN_ROOT'] + '|' + e['hook_event_name']}))"
        )
    )
    res = await run_hook(
        plugin,
        hook,
        {"hook_event_name": "PreToolUse"},
        cwd=str(tmp_path),
        default_timeout_sec=10,
    )
    assert res.additional_context == f"{plugin.root}|PreToolUse"


async def test_hook_failure_and_timeout_fail_open(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    crash = HookCommand(command=_cmd("import sys; sys.exit(1)"))
    res = await run_hook(
        plugin, crash, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed and not res.blocked

    slow = HookCommand(
        command=_cmd("import time; time.sleep(5)"), timeout_sec=1
    )
    res = await run_hook(
        plugin, slow, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed and not res.blocked


async def test_pre_tool_call_blocks_and_rewrites(tmp_path, monkeypatch):
    root = _make_plugin(
        tmp_path,
        hooks={
            "PreToolUse": [
                {
                    "matcher": "exec_command",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                "import json, sys; e = json.load(sys.stdin);"
                                "c = e['tool_input']['command'];"
                                "print(json.dumps({'decision': 'deny', 'reason': 'x'}) "
                                "if 'sudo' in c else json.dumps("
                                "{'updatedInput': {'command': c + ' --safe'}}))"
                            ),
                        }
                    ],
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")

    blocked = await runtime.apre_tool_call(
        ctx, state, "exec_command", json.dumps({"command": "sudo ls"})
    )
    out = json.loads(blocked.blocked_output)
    assert out["ok"] is False and out["error"] == runtime.BLOCKED_ERROR

    rewritten = await runtime.apre_tool_call(
        ctx, state, "exec_command", json.dumps({"command": "ls"})
    )
    assert rewritten.blocked_output is None
    assert json.loads(rewritten.arguments) == {"command": "ls --safe"}

    other = await runtime.apre_tool_call(
        ctx, state, "fs_read_file", json.dumps({"path": "a"})
    )
    assert json.loads(other.arguments) == {"path": "a"}


async def test_session_start_runs_once_and_is_skipped_when_durable(
    tmp_path, monkeypatch
):
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {
                    "matcher": "startup",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                'print(\'{"additionalContext": "HELLO"}\')'
                            ),
                        }
                    ],
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")

    first = await runtime.asession_start_messages(state, ctx)
    assert len(first) == 1 and "HELLO" in first[0]["content"]
    assert await runtime.asession_start_messages(state, ctx) == []

    durable_ctx = SimpleNamespace(durable_execution=True)
    assert (
        await runtime.asession_start_messages(
            SimpleNamespace(state_id="s2"), durable_ctx
        )
        == []
    )


async def test_no_plugins_configured_is_a_noop(monkeypatch):
    _config(monkeypatch, [])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")
    decision = await runtime.apre_tool_call(ctx, state, "t", "{}")
    assert decision.arguments == "{}" and decision.blocked_output is None
    assert await runtime.asession_start_messages(state, ctx) == []
    assert runtime.plugin_skill_roots(None) == []


async def test_plugin_skills_are_visible_between_system_and_user(tmp_path):
    root = _make_plugin(tmp_path, with_skill=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    reg = SkillsRegistry(
        system_root=str(empty),
        user_root=str(empty),
        project_root=str(empty),
        plugin_roots=[("demo", str(root / "skills"))],
    )
    skills = await reg.list_skills()
    assert "demo:demo-skill" in {
        s["id"] if isinstance(s, dict) else s.id for s in skills
    }


async def test_user_prompt_submit_adds_context_and_skips_technical(
    tmp_path, monkeypatch
):
    root = _make_plugin(
        tmp_path,
        hooks={
            "UserPromptSubmit": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                "import json,sys;d=json.load(sys.stdin);"
                                "print(json.dumps({'additionalContext':"
                                "'saw: '+d['prompt']}))"
                            ),
                        }
                    ]
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    state = SimpleNamespace(state_id="s1")
    out = await runtime.auser_prompt_messages(
        state, object(), [{"role": "user", "content": "hello"}]
    )
    assert len(out) == 1 and "saw: hello" in json.dumps(out[0])
    assert (
        await runtime.auser_prompt_messages(
            state, object(), [{"role": "assistant", "content": "x"}]
        )
        == []
    )


async def test_stop_hook_continues_with_cap(tmp_path, monkeypatch):
    root = _make_plugin(
        tmp_path,
        hooks={
            "Stop": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": "echo not done >&2; exit 2",
                        }
                    ]
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    state = SimpleNamespace(state_id="s1")
    for _ in range(runtime.MAX_STOP_CONTINUATIONS):
        out = await runtime.astop_decision(state, object(), "done")
        assert len(out) == 1 and "not done" in json.dumps(out[0])
    assert await runtime.astop_decision(state, object(), "done") == []


async def test_post_tool_failure_uses_failure_event(tmp_path, monkeypatch):
    log = tmp_path / "events.log"
    cmd = _cmd(
        "import json,sys;d=json.load(sys.stdin);"
        f"open({str(log)!r},'a').write(d['hook_event_name']+'\\n')"
    )
    root = _make_plugin(
        tmp_path,
        hooks={
            "PostToolUse": [{"hooks": [{"type": "command", "command": cmd}]}],
            "PostToolUseFailure": [
                {"hooks": [{"type": "command", "command": cmd}]}
            ],
        },
    )
    _config(monkeypatch, [str(root)])
    state = SimpleNamespace(state_id="s1")
    await runtime.apost_tool_call(
        state=state,
        ctx=object(),
        tool_name="t",
        arguments="{}",
        output='{"ok": true}',
    )
    await runtime.apost_tool_call(
        state=state,
        ctx=object(),
        tool_name="t",
        arguments="{}",
        output='{"ok": false}',
    )
    assert log.read_text().split() == ["PostToolUse", "PostToolUseFailure"]


def test_plugin_mcp_json_is_loaded_and_merged_into_config(tmp_path):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    root = _make_plugin(tmp_path, name="my plugin")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "local": {
                        "command": "${CLAUDE_PLUGIN_ROOT}/bin/srv",
                        "args": ["--x"],
                    },
                    "remote": {"type": "http", "url": "https://e.x/mcp"},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    out = resolve_paths(cfg, str(tmp_path))
    servers = out.mcp.servers
    assert set(servers) == {"my_plugin_local", "my_plugin_remote"}
    assert servers["my_plugin_local"].command == f"{root}/bin/srv"
    assert servers["my_plugin_local"].transport == "stdio"
    assert servers["my_plugin_remote"].url == "https://e.x/mcp"


def _git_repo(tmp_path):
    import subprocess

    src = _make_plugin(tmp_path, with_skill=True, name="gitplug")
    env = {
        "GIT_AUTHOR_NAME": "t",
        "GIT_AUTHOR_EMAIL": "t@t",
        "GIT_COMMITTER_NAME": "t",
        "GIT_COMMITTER_EMAIL": "t@t",
        "PATH": __import__("os").environ["PATH"],
    }
    for cmd in (
        ["init", "-q", "-b", "main"],
        ["add", "-A"],
        ["commit", "-q", "-m", "init"],
        ["tag", "v1"],
    ):
        subprocess.run(["git", *cmd], cwd=src, check=True, env=env)
    return src


def test_git_source_is_cloned_once_into_cache_and_loaded(
    tmp_path, monkeypatch
):
    from types import SimpleNamespace as NS

    from gc_core.plugins import sources

    repo = _git_repo(tmp_path)
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    src = NS(git=str(repo), ref="v1", subdir=None)
    path = sources.resolve_source(src)
    assert path is not None and (path / "skills").is_dir()
    # второй раз сеть/клон не нужны: каталог берётся из кэша
    import shutil

    shutil.rmtree(repo)
    sources._resolved.clear()
    assert sources.resolve_source(src) == path

    runtime.reset_plugin_cache()
    cfg = NS(paths=[], sources=[src], hook_timeout_sec=10)
    assert runtime.configured_paths(cfg) == [str(path)]


def test_git_source_failure_is_fail_open(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from gc_core.plugins import sources

    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    bad = NS(git=str(tmp_path / "nope"), ref=None, subdir=None)
    assert sources.resolve_source(bad) is None
    assert sources.resolve_sources([bad]) == []


def test_short_github_form_is_normalized():
    from gc_core.plugins.sources import normalize_url

    assert (
        normalize_url("obra/superpowers")
        == "https://github.com/obra/superpowers.git"
    )
    assert normalize_url("https://x.y/z.git") == "https://x.y/z.git"


def test_install_list_disable_enable_update_uninstall(tmp_path, monkeypatch):
    from gc_core.plugins import installed

    runtime.reset_plugin_cache()
    src = _make_plugin(tmp_path, with_skill=True, name="inst-demo")
    info = installed.install(str(src))
    assert info.name == "inst-demo" and info.enabled
    assert [p.name for p in installed.list_installed()] == ["inst-demo"]
    # установленный включён без правки конфига
    cfg = SimpleNamespace(paths=[], sources=[], hook_timeout_sec=10)
    assert len(runtime.configured_paths(cfg)) == 1

    with pytest.raises(ValueError):
        installed.install(str(src))  # уже установлен
    installed.install(str(src), force=True)

    assert installed.disable("inst-demo")
    assert runtime.configured_paths(cfg) == []
    assert not installed.list_installed()[0].enabled
    assert installed.enable("inst-demo")
    assert len(runtime.configured_paths(cfg)) == 1

    (src / "skills" / "demo-skill" / "SKILL.md").write_text(
        "---\nname: demo-skill\ndescription: d\n---\nnew body\n",
        encoding="utf-8",
    )
    assert installed.update("inst-demo").name == "inst-demo"
    copied = installed.installed_dir() / "inst-demo" / "skills" / "demo-skill"
    assert "new body" in (copied / "SKILL.md").read_text(encoding="utf-8")

    assert installed.uninstall("inst-demo")
    assert not installed.uninstall("inst-demo")
    assert installed.list_installed() == []


def test_install_rejects_non_plugin_dir(tmp_path):
    from gc_core.plugins import installed

    empty = tmp_path / "empty"
    empty.mkdir()
    with pytest.raises(ValueError):
        installed.install(str(empty))


def test_install_from_git_pins_commit(tmp_path, monkeypatch):
    from gc_core.plugins import installed, sources

    repo = _git_repo(tmp_path)
    info = installed.install(str(repo), ref=None)
    assert info.commit and len(info.commit) == 40


def test_gc_plugin_cli_roundtrip(tmp_path, capsys):
    from gc_core.plugins import cli

    src = _make_plugin(tmp_path, with_skill=True, name="cli-demo")
    assert cli.main(["install", str(src)]) == 0
    assert cli.main(["list"]) == 0
    assert "cli-demo" in capsys.readouterr().out
    assert cli.main(["disable", "cli-demo"]) == 0
    assert cli.main(["uninstall", "nope"]) == 1
    assert cli.main(["uninstall", "cli-demo"]) == 0


def test_cli_accepts_source_as_name(tmp_path, capsys):
    from gc_core.plugins import cli, installed

    src = _make_plugin(tmp_path, with_skill=True, name="by-src")
    assert cli.main(["install", str(src)]) == 0
    assert installed.resolve_name(str(src)) == "by-src"
    assert installed.resolve_name("by-src") == "by-src"
    assert cli.main(["disable", str(src)]) == 0
    assert cli.main(["uninstall", str(src)]) == 0
    assert installed.list_installed() == []


# --- команды и агенты плагинов -------------------------------------------


def _add_md(root, rel, text):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


REVIEW = """---
description: Проверить изменения
argument-hint: <файл>
allowed-tools: Read, Grep
---
Проверь $1 и ещё $2. Всё: $ARGUMENTS. Пропуск: [$3]."""

REVIEWER = """---
name: Code Reviewer
description: Проверяет код.
tools: Read, Grep, WebSearch
model: sonnet
color: red
---
Ты строгий ревьюер."""


def _plugin_with_md(tmp_path, name="demo"):
    root = _make_plugin(tmp_path, name=name)
    _add_md(root, "commands/review.md", REVIEW)
    _add_md(root, "agents/reviewer.md", REVIEWER)
    return root


def test_load_plugin_reads_commands_and_agents(tmp_path):
    plugin = load_plugin(_plugin_with_md(tmp_path))
    command = plugin.commands[0]
    assert command.name == "review"
    assert command.description == "Проверить изменения"
    assert command.argument_hint == "<файл>"
    assert command.allowed_tools == ("Read", "Grep")
    assert "$ARGUMENTS" in command.body
    agent = plugin.agents[0]
    assert agent.name == "Code Reviewer"
    assert agent.tools == ("Read", "Grep", "WebSearch")
    assert agent.body == "Ты строгий ревьюер."


def test_empty_command_and_agent_are_skipped(tmp_path):
    root = _make_plugin(tmp_path)
    _add_md(root, "commands/empty.md", "---\ndescription: x\n---\n")
    _add_md(root, "agents/empty.md", "---\nname: x\n---\n")
    plugin = load_plugin(root)
    assert plugin.commands == () and plugin.agents == ()


def test_subagent_name_is_valid_and_bounded():
    from gc_core.plugins.agents import subagent_name

    assert subagent_name("My-Plugin", "Code Reviewer") == (
        "subagent_my_plugin_code_reviewer"
    )
    long_name = subagent_name("p" * 40, "a" * 40)
    assert len(long_name) <= 64 and long_name.startswith("subagent_")
    assert long_name == subagent_name("p" * 40, "a" * 40)


def test_map_tools_translates_drops_and_keeps_native_names():
    from gc_core.plugins.agents import map_tools

    mapped, dropped = map_tools(
        [
            "Read",
            "Grep",
            "Bash",
            "WebSearch",
            "fs_list_files",
            "Edit",
            "MultiEdit",
        ]
    )
    assert mapped == (
        "fs_read_file",
        "fs_find_in_files",
        "exec_command",
        "fs_list_files",
        "fs_apply_file_edits",
    )
    assert dropped == ["WebSearch"]


def test_discovery_includes_plugin_agents(tmp_path, monkeypatch):
    from gc_core.tools.subagents.custom.file_definitions import (
        discover_subagent_definitions,
    )
    from gc_testkit.settings import make_settings

    root = _plugin_with_md(tmp_path)
    runtime.reset_plugin_cache()
    monkeypatch.setattr(installed, "enabled_paths", lambda root=None: [])
    settings = make_settings({"plugins.paths": [str(root)]})
    definitions = discover_subagent_definitions(
        settings, builtin_names={"subagent_worker"}
    )
    assert [d.name for d in definitions] == ["subagent_demo_code_reviewer"]
    d = definitions[0]
    assert d.tools == ("fs_read_file", "fs_find_in_files")
    assert d.role == "Ты строгий ревьюер." and d.stop_tools == ()


def test_discovery_skips_agent_without_known_tools(tmp_path, monkeypatch):
    from gc_core.tools.subagents.custom.file_definitions import (
        discover_subagent_definitions,
    )
    from gc_testkit.settings import make_settings

    root = _make_plugin(tmp_path)
    _add_md(
        root,
        "agents/web.md",
        "---\nname: web\ndescription: d\ntools: WebSearch, Task\n---\nРоль.",
    )
    runtime.reset_plugin_cache()
    monkeypatch.setattr(installed, "enabled_paths", lambda root=None: [])
    settings = make_settings({"plugins.paths": [str(root)]})
    assert discover_subagent_definitions(settings, builtin_names=set()) == []


def test_discovery_without_tools_key_means_all_tools(tmp_path, monkeypatch):
    from gc_core.tools.subagents.custom.file_definitions import (
        discover_subagent_definitions,
    )
    from gc_testkit.settings import make_settings

    root = _make_plugin(tmp_path)
    _add_md(root, "agents/any.md", "---\nname: any\n---\nРоль.")
    runtime.reset_plugin_cache()
    monkeypatch.setattr(installed, "enabled_paths", lambda root=None: [])
    settings = make_settings({"plugins.paths": [str(root)]})
    (d,) = discover_subagent_definitions(settings, builtin_names=set())
    assert d.tools is None and "демо" not in d.description.lower()
    assert d.description.startswith("Субагент плагина demo")


async def test_commands_are_skills_of_the_plugin_pack(tmp_path):
    root = _plugin_with_md(tmp_path)
    plugin = load_plugin(root)
    empty = tmp_path / "empty"
    empty.mkdir()
    reg = SkillsRegistry(
        system_root=str(empty),
        user_root=str(empty),
        project_root=str(empty),
        plugin_commands=[("demo", c) for c in plugin.commands],
    )
    skills = await reg.list_skills()
    ids = {s["id"] if isinstance(s, dict) else s.id for s in skills}
    assert "demo:review" in ids
    loaded = await reg.load_skill_detail("demo:review")
    assert "$ARGUMENTS" in loaded["instructions"]
    assert "<файл>" in loaded["instructions"]


def test_command_prompt_text_substitutes_arguments(tmp_path):
    plugin = load_plugin(_plugin_with_md(tmp_path))
    plugins = (plugin,)
    text = runtime.command_prompt_text(plugins, "/demo:review a.py b.py")
    assert "Проверь a.py и ещё b.py." in text
    assert "Всё: a.py b.py." in text and "Пропуск: []." in text
    assert "demo:review" in text
    # короткое имя, если оно одно
    assert runtime.command_prompt_text(plugins, "/review x") is not None
    # чужое и не команда
    assert runtime.command_prompt_text(plugins, "/other:review x") is None
    assert runtime.command_prompt_text(plugins, "/nope") is None
    assert runtime.command_prompt_text(plugins, "просто текст") is None


def test_ambiguous_short_command_name_is_not_expanded(tmp_path):
    a = load_plugin(_plugin_with_md(tmp_path, name="a"))
    b = load_plugin(_plugin_with_md(tmp_path, name="b"))
    assert runtime.command_prompt_text((a, b), "/review x") is None
    assert runtime.command_prompt_text((a, b), "/b:review x") is not None


async def test_user_prompt_expands_plugin_command(tmp_path, monkeypatch):
    root = _plugin_with_md(tmp_path)
    _config(monkeypatch, [str(root)])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")
    out = await runtime.auser_prompt_messages(
        state, ctx, [{"role": "user", "content": "/demo:review main.py"}]
    )
    assert len(out) == 1 and "Проверь main.py" in out[0]["content"]
    plain = await runtime.auser_prompt_messages(
        state, ctx, [{"role": "user", "content": "привет"}]
    )
    assert plain == []
