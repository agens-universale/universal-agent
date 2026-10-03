"""Паритет CLI/server с TUI: журнал правок пишется во всех режимах.

Обёртка-трекер ставится не только в TUI, поэтому правки из CLI/server-прогона
попадают в тот же журнал треда. Единственный гейт записи — thread-identity
(в CLI она есть только при ``-t``): без неё не пишется ничего и git не зовётся.
"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from gc_core.agent_edits import git_ops as edits_git_ops
from gc_core.agent_edits.journal import EditsJournal
from gc_core.agent_edits.tracking import TrackingFsBackend
from gc_core.config_modules import env_vars
from gc_core.run_context import acreate_run_context
from gc_testkit.settings import make_settings
from gc_core.thread_store import edits_journal_path
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_core.tools.subagents.sidecar import (
    bind_thread_identity,
    bind_turn_identity,
    clear_thread_identity,
)


@pytest.fixture
def roots(tmp_path, monkeypatch):
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    return launch_root, threads_dir


@pytest.fixture
def git_roots(roots):
    """launch_root — настоящий git-репо с одним коммитом (git-mode шелл-канала)."""
    launch_root, threads_dir = roots
    if shutil.which("git") is None:
        pytest.skip("git недоступен")
    for args in (
        ("init", "-b", "main"),
        ("config", "user.email", "test@example.com"),
        ("config", "user.name", "test"),
        ("config", "core.autocrlf", "false"),
    ):
        subprocess.run(
            ["git", *args],
            cwd=str(launch_root),
            check=True,
            capture_output=True,
        )
    (launch_root / "tracked.txt").write_bytes(b"line1\n")
    subprocess.run(
        ["git", "add", "-A"],
        cwd=str(launch_root),
        check=True,
        capture_output=True,
    )
    subprocess.run(
        ["git", "commit", "-m", "init"],
        cwd=str(launch_root),
        check=True,
        capture_output=True,
    )
    return launch_root, threads_dir


async def _cli_ctx(
    launch_root: Path, threads_dir: Path, *, identity: bool = True
):
    """RunContext так, как его собирает gc-cli с ВКЛЮЧЁННОЙ записью правок
    (`agent_edits.record.cli`): is_tui не передаётся (False), режим — `cli`.

    Identity вешается тем же способом, что AgentExecutor.arun(): при ``-t`` —
    bind_thread_identity, без треда — clear_thread_identity.
    """
    ctx = await acreate_run_context(
        make_settings(
            {
                "backend_api_mode": "chat_completions",
                "save_logs": False,
                "launch_root": str(launch_root),
                "run_mode": "cli",
                "agent_edits": {"record": {"cli": True}},
            }
        )
    )
    if identity:
        bind_thread_identity(
            ctx, threads_dir=str(threads_dir), system_id="cli", thread_id="th1"
        )
    else:
        clear_thread_identity(ctx)
    bind_turn_identity(ctx, "turn-1")
    return ctx


def _records(threads_dir: Path):
    return EditsJournal(
        edits_journal_path(str(threads_dir), "cli", "th1")
    ).read_all()


def _count_shell_git(monkeypatch) -> list[list[str]]:
    """Счётчик git-вызовов шелл-канала (единственная точка выхода в git)."""
    calls: list[list[str]] = []
    original = edits_git_ops.run_git

    def counting(launch_root, args, **kwargs):
        calls.append(list(args))
        return original(launch_root, args, **kwargs)

    monkeypatch.setattr(edits_git_ops, "run_git", counting)
    return calls


async def test_cli_context_wraps_local_backend_with_tracking(roots):
    launch_root, threads_dir = roots
    ctx = await _cli_ctx(launch_root, threads_dir)

    assert isinstance(ctx.os_backend, TrackingFsBackend)
    assert ctx.is_tui is False
    assert ctx.workspace.backend is ctx.os_backend


async def test_fs_edit_from_cli_context_lands_in_journal(roots):
    launch_root, threads_dir = roots
    ctx = await _cli_ctx(launch_root, threads_dir)
    target = launch_root / "app.py"

    res = json.loads(
        await WriteToFile(ctx=ctx, call_id="call-cli").acall(
            path=str(target), content="hello\n"
        )
    )
    assert res["ok"] is True

    recs = _records(threads_dir)
    assert [(r.path, r.op, r.call_id, r.turn_id) for r in recs] == [
        ("app.py", "create", "call-cli", "turn-1")
    ]


async def test_cli_context_without_identity_writes_no_journal(roots):
    launch_root, threads_dir = roots
    ctx = await _cli_ctx(launch_root, threads_dir, identity=False)
    target = launch_root / "app.py"

    res = json.loads(
        await WriteToFile(ctx=ctx, call_id="call-cli").acall(
            path=str(target), content="hello\n"
        )
    )
    assert res["ok"] is True
    assert target.read_bytes() == b"hello\n"
    assert list(threads_dir.iterdir()) == []


async def test_shell_command_from_cli_context_lands_in_journal(
    git_roots, monkeypatch
):
    launch_root, threads_dir = git_roots
    ctx = await _cli_ctx(launch_root, threads_dir)
    shell_git = _count_shell_git(monkeypatch)

    await ctx.os_backend.arun_python(
        "open('new.txt','wb').write(b'alpha\\n')", cwd=str(launch_root)
    )

    assert (launch_root / "new.txt").read_bytes() == b"alpha\n"
    assert [r.path for r in _records(threads_dir)] == ["new.txt"]
    assert shell_git != []


async def test_shell_channel_calls_no_git_without_identity(
    git_roots, monkeypatch
):
    launch_root, threads_dir = git_roots
    ctx = await _cli_ctx(launch_root, threads_dir, identity=False)
    shell_git = _count_shell_git(monkeypatch)

    await ctx.os_backend.arun_python(
        "open('new.txt','wb').write(b'alpha\\n')", cwd=str(launch_root)
    )

    assert (launch_root / "new.txt").read_bytes() == b"alpha\n"
    assert shell_git == []
    assert list(threads_dir.iterdir()) == []
