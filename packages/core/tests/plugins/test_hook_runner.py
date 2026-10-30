"""Запуск хука плагина внешней командой: вывод, коды выхода, таймаут."""

import asyncio
import json
import logging
import os
import shutil
import sys
import time

import pytest
from gc_core.plugins.hook_runner import MAX_CONTEXT_CHARS, run_hook
from gc_core.plugins.manifest import HookCommand, load_plugin

PY = sys.executable


def _make_plugin(tmp_path, name="demo"):
    root = tmp_path / name
    (root / ".claude-plugin").mkdir(parents=True)
    (root / ".claude-plugin" / "plugin.json").write_text(
        json.dumps({"name": name, "version": "1.2.3"}), encoding="utf-8"
    )
    return root


def _cmd(code: str) -> str:
    return f"{PY} -c {json.dumps(code)}"


async def _run(tmp_path, command: str):
    plugin = load_plugin(_make_plugin(tmp_path))
    return await run_hook(
        plugin,
        HookCommand(command=command),
        {},
        cwd=str(tmp_path),
        default_timeout_sec=10,
    )


async def test_plain_text_stdout_is_context(tmp_path):
    res = await _run(tmp_path, _cmd("print('just text')"))
    assert res.additional_context == "just text"
    assert not res.failed and not res.blocked


async def test_json_decision_block_blocks_with_reason(tmp_path):
    res = await _run(
        tmp_path, _cmd('print(\'{"decision": "block", "reason": "r"}\')')
    )
    assert res.blocked and res.reason == "r"


async def test_json_without_context_adds_nothing(tmp_path):
    res = await _run(tmp_path, _cmd("print('{\"continue\": true}')"))
    assert res.additional_context == ""


async def test_long_context_is_truncated_with_mark(tmp_path):
    res = await _run(tmp_path, _cmd(f"print('x' * {MAX_CONTEXT_CHARS * 2})"))
    assert res.additional_context.startswith("x" * MAX_CONTEXT_CHARS)
    assert "x" * (MAX_CONTEXT_CHARS + 1) not in res.additional_context
    assert len(res.additional_context) < MAX_CONTEXT_CHARS + 200


async def test_hook_exit_code_2_blocks_with_stderr_reason(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    hook = HookCommand(
        command=_cmd("import sys; sys.stderr.write('nope'); sys.exit(2)")
    )
    res = await run_hook(
        plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.blocked and res.reason == "nope"


async def test_hook_both_context_formats(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
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
        {"hook_event_name": "SessionStart"},
        cwd=str(tmp_path),
        default_timeout_sec=10,
    )
    assert res.additional_context == f"{plugin.root}|SessionStart"


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
        text = pid_file.read_text().strip() if pid_file.exists() else ""
        if text:
            return int(text)
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
async def test_timeout_kills_child_after_bash_exit(tmp_path):
    plugin = load_plugin(_make_plugin(tmp_path))
    pid_file = tmp_path / "pid"
    hook = HookCommand(
        command=f"sleep 600 & echo $! > {pid_file}; echo x", timeout_sec=1
    )
    res = await run_hook(
        plugin, hook, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed
    assert await _wait_dead(await _read_pid(pid_file))


@_posix_only
async def test_killpg_permission_error_keeps_contract(tmp_path, monkeypatch):
    real_killpg = os.killpg

    def killpg_eperm(pid, sig):
        real_killpg(pid, sig)
        raise PermissionError

    monkeypatch.setattr(os, "killpg", killpg_eperm)
    plugin = load_plugin(_make_plugin(tmp_path))
    pid_file = tmp_path / "pid"
    slow = HookCommand(command=_pid_command(pid_file), timeout_sec=1)
    res = await run_hook(
        plugin, slow, {}, cwd=str(tmp_path), default_timeout_sec=10
    )
    assert res.failed
    pid_file.unlink()
    task = asyncio.create_task(
        run_hook(
            plugin,
            HookCommand(command=_pid_command(pid_file)),
            {},
            cwd=str(tmp_path),
            default_timeout_sec=30,
        )
    )
    await _read_pid(pid_file)
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
