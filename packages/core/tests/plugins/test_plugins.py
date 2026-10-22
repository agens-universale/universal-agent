"""Песочница плагинов: манифест, запуск хуков, фасад для цикла и skills."""

import asyncio
import json
import logging
import os
import shutil
import sys
import time

import pytest
from types import SimpleNamespace

from gc_core.plugins import installed, runtime
from gc_core.plugins.hook_runner import run_hook
from gc_core.plugins.manifest import (
    HookCommand,
    load_plugin,
    validate_ap_manifest,
)
from gc_core.tools.skills.registry import SkillsRegistry

PY = sys.executable
AP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/plugin.schema.json"
AP_MCP_SCHEMA = "https://agent-plugins.org/schemas/1.0.0/mcp.schema.json"


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


_posix_only = pytest.mark.skipif(
    sys.platform == "win32", reason="группы процессов только в POSIX"
)


def _pid_command(pid_file) -> str:
    return f"sleep 600 & echo $! > {pid_file}; sleep 600"


async def _wait_dead(pid: int, limit: float = 2.0) -> bool:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        await asyncio.sleep(0.05)
    return False


async def _read_pid(pid_file, limit: float = 5.0) -> int:
    deadline = time.monotonic() + limit
    while time.monotonic() < deadline:
        if pid_file.exists() and pid_file.read_text().strip():
            return int(pid_file.read_text())
        await asyncio.sleep(0.05)
    raise AssertionError("pid file was not written")


@_posix_only
async def test_timeout_kills_process_group(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    pid_file = tmp_path / "pid"
    hook = HookCommand(command=_pid_command(pid_file), timeout_sec=1)
    res = await run_hook(
        plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed
    assert await _wait_dead(await _read_pid(pid_file))


@_posix_only
async def test_cancel_kills_hook(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    pid_file = tmp_path / "pid"
    hook = HookCommand(command=_pid_command(pid_file))
    task = asyncio.create_task(
        run_hook(plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=30)
    )
    pid = await _read_pid(pid_file)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task
    assert await _wait_dead(pid)


@_posix_only
async def test_killpg_permission_error_keeps_contract(tmp_path, monkeypatch):
    real_killpg = os.killpg

    def killpg_eperm(pid, sig):
        real_killpg(pid, sig)
        raise PermissionError

    monkeypatch.setattr(os, "killpg", killpg_eperm)
    plugin = load_plugin(_make_plugin(tmp_path))
    slow = HookCommand(command="sleep 600", timeout_sec=1)
    res = await run_hook(
        plugin, slow, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed
    task = asyncio.create_task(
        run_hook(
            plugin,
            HookCommand(command="sleep 600"),
            {},
            cwd=str(tmp_path),
            default_timeout_sec=30,
        )
    )
    await asyncio.sleep(0.3)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task


async def test_no_bash_fails_open(tmp_path, monkeypatch, caplog):
    from gc_core.plugins import hook_runner

    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(shutil, "which", lambda name: None)
    monkeypatch.setattr(hook_runner, "_bash_warned", False)
    plugin = load_plugin(_make_plugin(tmp_path))
    hook = HookCommand(command="true")
    with caplog.at_level(logging.WARNING):
        for _ in range(2):
            res = await run_hook(
                plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
            )
            assert res.failed
    assert caplog.text.count("plugin hooks need bash in PATH") == 1


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


@pytest.mark.parametrize("matcher", ["Bash", "exec_command"])
async def test_tool_hook_matches_claude_name(tmp_path, monkeypatch, matcher):
    stdin_log = tmp_path / "stdin.json"
    root = _make_plugin(
        tmp_path,
        hooks={
            "PreToolUse": [
                {
                    "matcher": matcher,
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                "import sys;"
                                f"open({str(stdin_log)!r}, 'w').write(sys.stdin.read());"
                                "sys.stderr.write('no'); sys.exit(2)"
                            ),
                        }
                    ],
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")
    decision = await runtime.apre_tool_call(
        ctx, state, "exec_command", json.dumps({"command": "ls"})
    )
    assert decision.blocked_output is not None
    assert json.loads(stdin_log.read_text())["tool_name"] == "Bash"


async def test_pre_tool_matcher_uses_every_claude_alias(tmp_path, monkeypatch):
    root = _make_plugin(
        tmp_path,
        hooks={
            "PreToolUse": [
                {
                    "matcher": "MultiEdit",
                    "hooks": [{"type": "command", "command": "exit 2"}],
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    decision = await runtime.apre_tool_call(
        SimpleNamespace(),
        SimpleNamespace(state_id="s1"),
        "fs_apply_file_edits",
        "{}",
    )
    assert decision.blocked_output is not None


async def test_post_tool_hook_matches_claude_name(tmp_path, monkeypatch):
    log = tmp_path / "post.log"
    root = _make_plugin(
        tmp_path,
        hooks={
            "PostToolUse": [
                {
                    "matcher": "Bash",
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                "import json,sys;d=json.load(sys.stdin);"
                                f"open({str(log)!r},'w').write(d['tool_name'])"
                            ),
                        }
                    ],
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    await runtime.apost_tool_call(
        SimpleNamespace(),
        SimpleNamespace(state_id="s1"),
        "exec_command",
        "{}",
        '{"ok": true}',
    )
    assert log.read_text() == "Bash"


async def test_session_start_without_output_runs_once_per_loop(
    tmp_path, monkeypatch
):
    counter = tmp_path / "count.txt"
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                f"open({str(counter)!r}, 'a').write('x\\n')"
                            ),
                        }
                    ]
                }
            ]
        },
    )
    _config(monkeypatch, [str(root)])
    ctx = SimpleNamespace()
    state = SimpleNamespace(state_id="s1", messages=[])
    started = False
    for _ in range(3):
        messages, started = await runtime.asession_start_messages(
            state, ctx, started
        )
        assert messages == []
    assert counter.read_text().split() == ["x"]


async def test_session_start_ignores_non_dict_messages(tmp_path, monkeypatch):
    _config(monkeypatch, [str(_make_plugin(tmp_path, hooks={}))])
    state = SimpleNamespace(state_id="s1", messages=["raw", None])
    assert await runtime.asession_start_messages(state, SimpleNamespace()) == (
        [],
        True,
    )


def test_reset_plugin_cache_clears_source_warnings():
    from gc_core.plugins import sources

    sources._warned.add(("a", "b", "c"))
    runtime.reset_plugin_cache()
    assert not sources._warned


async def test_session_start_not_repeated_for_loaded_history(
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
    ctx = SimpleNamespace()
    state = SimpleNamespace(state_id="s1", messages=[])
    keys = set(vars(state))

    first, _ = await runtime.asession_start_messages(state, ctx)
    assert len(first) == 1 and "HELLO" in first[0]["content"]
    assert first[0]["id"].startswith(runtime.SESSION_START_ID_PREFIX)

    # Новый State следующего хода: история подгружена из треда.
    next_state = SimpleNamespace(state_id="s2", messages=first)
    assert await runtime.asession_start_messages(next_state, ctx) == ([], True)
    assert set(vars(state)) == keys
    assert set(vars(next_state)) == {"state_id", "messages"}

    durable_ctx = SimpleNamespace(durable_execution=True)
    assert await runtime.asession_start_messages(
        SimpleNamespace(state_id="s3", messages=[]), durable_ctx
    ) == ([], True)


async def test_session_start_and_stop_skipped_in_subagent(
    tmp_path, monkeypatch
):
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {
                    "hooks": [
                        {
                            "type": "command",
                            "command": _cmd(
                                'print(\'{"additionalContext": "HELLO"}\')'
                            ),
                        }
                    ]
                }
            ],
            "Stop": [{"hooks": [{"type": "command", "command": "exit 2"}]}],
        },
    )
    _config(monkeypatch, [str(root)])
    state = SimpleNamespace(state_id="s1", messages=[], parent_state_id="p")
    assert await runtime.asession_start_messages(state, SimpleNamespace()) == (
        [],
        True,
    )
    assert await runtime.astop_decision(state, object(), "done", 0) == ([], 0)


async def test_no_plugins_configured_is_a_noop(monkeypatch):
    _config(monkeypatch, [])
    ctx, state = SimpleNamespace(), SimpleNamespace(state_id="s1")
    decision = await runtime.apre_tool_call(ctx, state, "t", "{}")
    assert decision.arguments == "{}" and decision.blocked_output is None
    assert await runtime.asession_start_messages(state, ctx) == ([], True)
    assert runtime.plugin_skill_roots(None) == []


async def test_plugin_skills_are_visible_between_system_and_user(tmp_path):
    root = _make_plugin(tmp_path, with_skill=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    reg = SkillsRegistry(
        system_root=str(empty),
        user_root=str(empty),
        project_root=str(empty),
        plugin_roots=[("demo", str(root / "skills"), None)],
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
    used = 0
    for _ in range(runtime.MAX_STOP_CONTINUATIONS):
        out, new_used = await runtime.astop_decision(
            state, object(), "done", used
        )
        assert len(out) == 1 and "not done" in json.dumps(out[0])
        assert new_used == used + 1
        used = new_used
    assert await runtime.astop_decision(state, object(), "done", used) == (
        [],
        0,
    )


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


def test_plugin_mcp_env_vars_are_expanded(tmp_path, monkeypatch):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    monkeypatch.setenv("GC_T_TOKEN", "abc")
    monkeypatch.delenv("GC_T_PATH", raising=False)
    root = _make_plugin(tmp_path, name="p")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "gh": {
                    "type": "http",
                    "url": "https://x/${GC_T_PATH:-mcp}",
                    "headers": {"Authorization": "Bearer ${GC_T_TOKEN}"},
                }
            }
        ),
        encoding="utf-8",
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    out = resolve_paths(cfg, str(tmp_path))
    server = out.mcp.servers["p_gh"]
    assert server.headers == {"Authorization": "Bearer abc"}
    assert server.url == "https://x/mcp"


def test_plugin_mcp_missing_env_var_skips_server(
    tmp_path, monkeypatch, caplog
):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    monkeypatch.delenv("GC_T_MISSING", raising=False)
    root = _make_plugin(tmp_path, name="p")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "bad": {
                    "type": "http",
                    "url": "https://x/mcp",
                    "headers": {"Authorization": "Bearer ${GC_T_MISSING}"},
                },
                "ok": {"command": "echo"},
            }
        ),
        encoding="utf-8",
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    with caplog.at_level("WARNING"):
        out = resolve_paths(cfg, str(tmp_path))
    assert set(out.mcp.servers) == {"p_ok"}
    assert "GC_T_MISSING" in caplog.text
    assert "'bad'" in caplog.text


def test_bad_plugin_mcp_server_is_skipped(tmp_path):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    root = _make_plugin(tmp_path, name="p")
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "bad": {"command": "echo", "args": "--x"},
                "good": {"command": "echo"},
            }
        ),
        encoding="utf-8",
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    out = resolve_paths(cfg, str(tmp_path))
    assert set(out.mcp.servers) == {"p_good"}


def test_bad_plugin_mcp_server_warning_has_no_secret_values(tmp_path, caplog):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    root = _make_plugin(tmp_path, name="p")
    (root / ".mcp.json").write_text(
        json.dumps({"bad": {"command": "echo", "args": "SECRET123"}}),
        encoding="utf-8",
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    with caplog.at_level("WARNING"):
        resolve_paths(cfg, str(tmp_path))
    assert "p_bad" in caplog.text
    assert "SECRET123" not in caplog.text


def test_agent_plugins_layout_mcp_json(tmp_path):
    root = tmp_path / "ap"
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AP_SCHEMA, "name": "ap", "version": "1"}),
        encoding="utf-8",
    )
    (root / "mcp.json").write_text(
        json.dumps(
            {
                "$schema": AP_MCP_SCHEMA,
                "mcpServers": {"s": {"type": "stdio", "command": "echo"}},
            }
        ),
        encoding="utf-8",
    )
    assert "s" in load_plugin(root).mcp_servers
    installed.install(str(root))


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


def test_runtime_does_not_clone(tmp_path, monkeypatch, caplog):
    from types import SimpleNamespace as NS

    from gc_core.plugins import sources

    def boom(*a, **k):
        raise AssertionError("clone on hot path")

    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    monkeypatch.setattr(sources, "_clone", boom)
    sources._resolved.clear()
    src = NS(git=str(tmp_path / "repo"), ref=None, subdir=None)
    cfg = NS(paths=[], sources=[src], hook_timeout_sec=10, use_installed=False)
    assert runtime.configured_paths(cfg) == []
    assert sources._resolved == {}
    assert str(tmp_path / "repo") in caplog.text


def test_runtime_uses_cached_source_without_fetch(tmp_path, monkeypatch):
    from types import SimpleNamespace as NS

    from gc_core.plugins import sources

    repo = _git_repo(tmp_path)
    monkeypatch.setattr(sources, "cache_root", lambda: tmp_path / "cache")
    sources._resolved.clear()
    src = NS(git=str(repo), ref="v1", subdir=None)
    cfg = NS(paths=[], sources=[src], hook_timeout_sec=10, use_installed=False)
    assert runtime.configured_paths(cfg) == []
    assert runtime.configured_paths(cfg, fetch=True) != []
    sources._resolved.clear()

    def boom(*a, **k):
        raise AssertionError("clone on hot path")

    monkeypatch.setattr(sources, "_clone", boom)
    assert len(runtime.configured_paths(cfg)) == 1


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
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert cli.main(["list"]) == 0
    assert "cli-demo" in capsys.readouterr().out
    assert cli.main(["disable", "cli-demo"]) == 0
    assert cli.main(["uninstall", "nope"]) == 1
    assert cli.main(["uninstall", "cli-demo"]) == 0


def test_cli_accepts_source_as_name(tmp_path, capsys):
    from gc_core.plugins import cli, installed

    src = _make_plugin(tmp_path, with_skill=True, name="by-src")
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert installed.resolve_name(str(src)) == "by-src"
    assert installed.resolve_name("by-src") == "by-src"
    assert cli.main(["disable", str(src)]) == 0
    assert cli.main(["uninstall", str(src)]) == 0
    assert installed.list_installed() == []


def _mcp_plugin(tmp_path, name="trust"):
    root = _make_plugin(
        tmp_path,
        hooks={
            "SessionStart": [
                {"hooks": [{"type": "command", "command": "echo hi-hook"}]}
            ]
        },
        name=name,
    )
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "srv": {
                        "command": "npx",
                        "args": ["-y", "pkg"],
                        "env": {"TOKEN": "s3cret"},
                    }
                }
            }
        ),
        encoding="utf-8",
    )
    return root


def _tty(monkeypatch, answers, is_tty=True):
    asked = []
    monkeypatch.setattr("sys.stdin.isatty", lambda: is_tty)
    monkeypatch.setattr(
        "builtins.input",
        lambda prompt="": asked.append(prompt) or answers.pop(0),
    )
    return asked


def test_install_shows_summary_and_asks(tmp_path, monkeypatch, capsys):
    from gc_core.plugins import cli

    src = _mcp_plugin(tmp_path)
    _tty(monkeypatch, ["n", "y"])
    assert cli.main(["install", str(src)]) == 1
    out = capsys.readouterr().out
    assert "echo hi-hook" in out and "mcp srv: npx -y pkg" in out
    assert "s3cret" not in out
    assert installed.list_installed() == []
    assert cli.main(["install", str(src)]) == 0
    assert [p.name for p in installed.list_installed()] == ["trust"]


def test_install_without_tty_requires_yes(tmp_path, monkeypatch):
    from gc_core.plugins import cli

    src = _mcp_plugin(tmp_path)
    _tty(monkeypatch, [], is_tty=False)
    assert cli.main(["install", str(src)]) == 1
    assert installed.list_installed() == []
    assert cli.main(["install", "--yes", str(src)]) == 0
    assert len(installed.list_installed()) == 1


def test_update_asks_only_when_hooks_or_mcp_change(tmp_path, monkeypatch):
    from gc_core.plugins import cli

    src = _mcp_plugin(tmp_path)
    assert cli.main(["install", "--yes", str(src)]) == 0
    asked = _tty(monkeypatch, ["y"])
    assert cli.main(["update", "trust"]) == 0
    assert asked == []
    hooks = json.loads((src / "hooks" / "hooks.json").read_text())
    hooks["hooks"]["Stop"] = [
        {"hooks": [{"type": "command", "command": "echo new-hook"}]}
    ]
    (src / "hooks" / "hooks.json").write_text(json.dumps(hooks))
    assert cli.main(["update", "trust"]) == 0
    assert len(asked) == 1


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


def test_use_installed_false_keeps_explicit_paths(monkeypatch):
    monkeypatch.setattr(installed, "enabled_paths", lambda: ["/inst/a"])
    off = SimpleNamespace(paths=["/p"], sources=[], use_installed=False)
    on = SimpleNamespace(paths=["/p"], sources=[], use_installed=True)
    assert runtime.configured_paths(off) == ["/p"]
    assert runtime.configured_paths(on) == ["/p", "/inst/a"]


def _ap_root(tmp_path, manifest, name="ap"):
    root = tmp_path / name
    (root / "skills" / "a").mkdir(parents=True)
    (root / "skills" / "a" / "SKILL.md").write_text(
        "---\nname: a\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (root / "plugin.json").write_text(json.dumps(manifest), encoding="utf-8")
    return root


def test_ap_manifest_valid(tmp_path):
    data = {
        "$schema": AP_SCHEMA,
        "name": "agent-plugins-example",
        "version": "1.0.0",
    }
    assert validate_ap_manifest(data) == []
    assert load_plugin(_ap_root(tmp_path, data)).format == "agent-plugins"


def test_ap_manifest_missing_schema_rejected(tmp_path):
    root = _ap_root(tmp_path, {"name": "x"})
    assert load_plugin(root) is None
    with pytest.raises(ValueError, match=r"\$schema"):
        installed.install(str(root))


def test_ap_manifest_bad_name_rejected():
    assert validate_ap_manifest({"$schema": AP_SCHEMA, "name": "Bad--Name"})


def test_ap_unknown_field_reported(tmp_path, caplog):
    root = _ap_root(tmp_path, {"$schema": AP_SCHEMA, "name": "x", "hooks": {}})
    with caplog.at_level("WARNING"):
        plugin = load_plugin(root)
    assert plugin is not None
    assert "hooks" in caplog.text


async def test_ap_skill_symlink_outside_root_skipped(tmp_path):
    root = _ap_root(tmp_path, {"$schema": AP_SCHEMA, "name": "x"})
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "SKILL.md").write_text(
        "---\nname: evil\ndescription: d\n---\nbody\n", encoding="utf-8"
    )
    (root / "skills" / "evil").symlink_to(outside, target_is_directory=True)
    empty = tmp_path / "empty"
    empty.mkdir()
    reg = SkillsRegistry(
        system_root=str(empty),
        user_root=str(empty),
        project_root=str(empty),
        plugin_roots=[("x", str(root / "skills"), str(root))],
    )
    ids = {
        s["id"] if isinstance(s, dict) else s.id
        for s in await reg.list_skills()
    }
    assert "x:a" in ids
    assert "x:evil" not in ids


def test_cc_plugin_unaffected_by_ap_rules(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    assert plugin.format == "claude-code"


def test_both_manifests_prefer_claude_code(tmp_path):
    root = _make_plugin(tmp_path, name="cc", with_skill=True)
    (root / "plugin.json").write_text(
        json.dumps({"name": "other"}), encoding="utf-8"
    )
    plugin = load_plugin(root)
    assert (plugin.format, plugin.name) == ("claude-code", "cc")
    assert plugin.skills_dir is not None


def _ap_plugin(tmp_path, servers, name="apmcp", schema=AP_MCP_SCHEMA):
    root = tmp_path / name
    root.mkdir()
    (root / "plugin.json").write_text(
        json.dumps({"$schema": AP_SCHEMA, "name": name, "version": "1"}),
        encoding="utf-8",
    )
    doc = {"mcpServers": servers}
    if schema:
        doc["$schema"] = schema
    (root / "mcp.json").write_text(json.dumps(doc), encoding="utf-8")
    return root


def test_ap_mcp_streamable_http(tmp_path):
    from gc_core.config_modules.root import AppConfig, resolve_paths

    root = _ap_plugin(
        tmp_path,
        {"api": {"type": "streamable-http", "url": "https://e.x/mcp"}},
    )
    runtime.reset_plugin_cache()
    cfg = AppConfig.model_validate({"plugins": {"paths": [str(root)]}})
    servers = resolve_paths(cfg, str(tmp_path)).mcp.servers
    assert servers["apmcp_api"].transport == "stream_http"


def test_claude_code_mcp_streamable_http_maps_to_stream_http(tmp_path):
    root = _make_plugin(tmp_path)
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "r": {"type": "streamable-http", "url": "https://e.x/m"}
                }
            }
        ),
        encoding="utf-8",
    )
    assert load_plugin(root).mcp_servers["r"]["transport"] == "stream_http"


def test_ap_mcp_plugin_root_and_data(tmp_path):
    root = _ap_plugin(
        tmp_path,
        {
            "s": {
                "type": "stdio",
                "command": "node",
                "args": ["${PLUGIN_ROOT}/srv.js"],
                "env": {"D": "${PLUGIN_DATA}"},
            }
        },
    )
    entry = load_plugin(root).mcp_servers["s"]
    data = installed.plugin_data_dir("apmcp")
    assert entry["args"] == [f"{root.resolve()}/srv.js"]
    assert entry["env"]["D"] == str(data)
    assert entry["env"]["PLUGIN_ROOT"] == str(root.resolve())
    assert entry["env"]["PLUGIN_DATA"] == str(data)
    assert data.is_dir()


def test_ap_mcp_does_not_expand_env(tmp_path, monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    root = _ap_plugin(
        tmp_path,
        {
            "s": {"type": "stdio", "command": "node", "args": ["${HOME}"]},
            "h": {
                "type": "streamable-http",
                "url": "https://e.x/mcp",
                "headers": {"A": "${GITHUB_TOKEN}"},
            },
        },
    )
    servers = load_plugin(root).mcp_servers
    assert servers["s"]["args"] == ["${HOME}"]
    assert servers["h"]["headers"] == {"A": "${GITHUB_TOKEN}"}


def test_ap_mcp_rejects_reserved_env_and_plain_http(tmp_path):
    root = _ap_plugin(
        tmp_path,
        {
            "env": {
                "type": "stdio",
                "command": "node",
                "env": {"PLUGIN_ROOT": "x"},
            },
            "plain": {
                "type": "streamable-http",
                "url": "http://example.com/mcp",
            },
            "loop": {
                "type": "streamable-http",
                "url": "http://127.0.0.1:9/mcp",
            },
            "mixed": {
                "type": "stdio",
                "command": "node",
                "url": "https://e.x",
            },
            "notype": {"command": "node"},
        },
    )
    assert set(load_plugin(root).mcp_servers) == {"loop"}


def test_ap_mcp_requires_schema_and_ignores_dot_mcp_json(tmp_path):
    root = _ap_plugin(
        tmp_path,
        {"s": {"type": "stdio", "command": "node"}},
        schema=None,
    )
    (root / ".mcp.json").write_text(
        json.dumps({"mcpServers": {"x": {"command": "y"}}}), encoding="utf-8"
    )
    assert load_plugin(root).mcp_servers == {}


def test_claude_code_mcp_http_means_streamable_http(tmp_path):
    root = _make_plugin(tmp_path)
    (root / ".mcp.json").write_text(
        json.dumps(
            {
                "mcpServers": {
                    "r": {"type": "http", "url": "https://e.x/mcp"},
                    "r2": {"url": "https://e.x/mcp"},
                    "r3": {"type": "sse", "url": "https://e.x/sse"},
                }
            }
        ),
        encoding="utf-8",
    )
    servers = load_plugin(root).mcp_servers
    assert servers["r"]["transport"] == "stream_http"
    assert servers["r2"]["transport"] == "stream_http"
    assert servers["r3"]["transport"] == "sse"


def test_ap_mcp_rejects_bad_cwd_and_command(tmp_path):
    stdio = {"type": "stdio"}
    root = _ap_plugin(
        tmp_path,
        {
            "cwd_escape": {**stdio, "command": "n", "cwd": "./../x"},
            "cwd_root_escape": {
                **stdio,
                "command": "n",
                "cwd": "${PLUGIN_ROOT}/../x",
            },
            "cwd_form": {**stdio, "command": "n", "cwd": "/abs"},
            "cmd_slash": {**stdio, "command": "bin/srv"},
            "cmd_escape": {**stdio, "command": "./../srv"},
            "ok": {
                **stdio,
                "command": "./bin/srv",
                "cwd": "${PLUGIN_DATA}/w",
            },
        },
    )
    assert set(load_plugin(root).mcp_servers) == {"ok"}


def test_ap_mcp_sse_accepted_and_duplicate_headers_rejected(tmp_path):
    root = _ap_plugin(
        tmp_path,
        {
            "legacy": {"type": "sse", "url": "https://e.x/sse"},
            "dup": {
                "type": "streamable-http",
                "url": "https://e.x/mcp",
                "headers": {"X-A": "1", "x-a": "2"},
            },
        },
    )
    servers = load_plugin(root).mcp_servers
    assert set(servers) == {"legacy"}
    assert servers["legacy"]["transport"] == "sse"


def test_ap_mcp_data_dir_created_only_for_valid_stdio(tmp_path):
    root = _ap_plugin(
        tmp_path,
        {"bad": {"type": "stdio", "command": "bin/x"}},
        name="lazydata",
    )
    load_plugin(root)
    assert not installed.plugin_data_path("lazydata").exists()


def test_ap_mcp_invalid_file_warning_has_reason(tmp_path, caplog):
    root = _ap_plugin(tmp_path, {}, name="noschema", schema=None)
    with caplog.at_level("WARNING"):
        load_plugin(root)
    assert "missing or wrong $schema" in caplog.text
