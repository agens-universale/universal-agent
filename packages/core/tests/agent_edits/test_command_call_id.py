"""Правки команды привязаны к её вызову: поле ``command_call_id``."""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from gc_core.agent_edits import shell_delta
from gc_core.agent_edits.journal import EditRecord, EditsJournal
from gc_core.agent_edits.shell_delta import ShellDelta
from gc_core.agent_edits.tracking import wrap_local_for_tracking
from gc_core.config_modules import env_vars
from gc_core.execution.tool_call_context import bind_tool_call
from gc_core.execution_workspace import build_execution_workspace
from gc_core.thread_store import edits_journal_path
from gc_core.tools.fs.write_to_file_tool import WriteToFile
from gc_testkit.helpers.tool_ctx import local_ctx


def _git(root: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args], cwd=str(root), check=True, capture_output=True
    )


@pytest.fixture
def repo(tmp_path, monkeypatch):
    if not shutil.which("git"):
        pytest.skip("git недоступен")
    root = tmp_path / "proj"
    root.mkdir()
    threads = tmp_path / "threads"
    threads.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    monkeypatch.chdir(root)
    _git(root, "init", "-b", "main")
    _git(root, "config", "user.email", "test@example.com")
    _git(root, "config", "user.name", "test")
    (root / "tracked.txt").write_bytes(b"line1\n")
    _git(root, "add", "-A")
    _git(root, "commit", "-m", "init")
    return root, threads


def _ctx(root: Path, threads: Path):
    ctx = local_ctx(
        {
            "launch_root": str(root),
            "security.write_guard": {
                "enabled": False,
                "mode": "deny",
                "extra_paths": [],
            },
            "agent_edits": {},
        }
    )
    ctx.is_tui = True
    ctx.threads_dir = str(threads)
    ctx.system_id = "tui"
    ctx.thread_id = "t1"
    ctx.turn_id = "turn-1"
    wrapped = wrap_local_for_tracking(ctx.os_backend, ctx)
    ctx.os_backend = wrapped
    ctx.workspace = build_execution_workspace(wrapped, ctx)
    return ctx


def _records(threads: Path):
    return EditsJournal(
        edits_journal_path(str(threads), "tui", "t1")
    ).read_all()


_WRITE_TWO = (
    "open('a.txt','wb').write(b'a\\n'); open('b.txt','wb').write(b'b\\n')"
)


async def test_shell_records_carry_the_bound_command_call(repo):
    root, threads = repo
    ctx = _ctx(root, threads)
    with bind_tool_call("exec_command", "c1"):
        await ctx.os_backend.arun_python(_WRITE_TWO, cwd=str(root))

    records = _records(threads)
    assert sorted(r.path for r in records) == ["a.txt", "b.txt"]
    assert [(r.call_id, r.command_call_id) for r in records] == [
        (None, "c1"),
        (None, "c1"),
    ]


async def test_shell_records_without_binding_have_no_command_call(repo):
    root, threads = repo
    ctx = _ctx(root, threads)
    await ctx.os_backend.arun_python(_WRITE_TWO, cwd=str(root))

    assert [r.command_call_id for r in _records(threads)] == [None, None]


async def test_pocket_records_carry_the_bound_command_call(repo):
    """Карманный подканал (путь уже в наборе) пишет то же поле."""
    root, threads = repo
    ctx = _ctx(root, threads)
    target = root / "tracked.txt"
    await WriteToFile(ctx=ctx, call_id="w1").acall(
        path=str(target), content="dirty\n"
    )
    with bind_tool_call("exec_command", "c2"):
        await ctx.os_backend.arun_python(
            "open('tracked.txt','wb').write(b'after\\n')", cwd=str(root)
        )

    shell = [r for r in _records(threads) if r.call_id is None]
    assert [(r.path, r.command_call_id) for r in shell] == [
        ("tracked.txt", "c2")
    ]


async def test_capped_command_records_carry_both_fields(repo, monkeypatch):
    root, threads = repo
    monkeypatch.setattr(shell_delta, "MAX_PATHS_PER_COMMAND", 1)
    ctx = _ctx(root, threads)
    with bind_tool_call("exec_command", "c3"):
        await ctx.os_backend.arun_python(_WRITE_TWO, cwd=str(root))

    records = _records(threads)
    assert [(r.command_paths, r.command_call_id) for r in records] == [
        (2, "c3")
    ]


async def test_fs_tool_record_has_no_command_call(repo):
    root, threads = repo
    ctx = _ctx(root, threads)
    with bind_tool_call("fs_write_file", "w2"):
        await WriteToFile(ctx=ctx, call_id="w2").acall(
            path=str(root / "x.txt"), content="x\n"
        )

    assert [(r.call_id, r.command_call_id) for r in _records(threads)] == [
        ("w2", None)
    ]


def test_begin_takes_command_call_id_explicitly(repo):
    root, threads = repo
    ctx = _ctx(root, threads)
    delta = ShellDelta.begin(ctx, command_call_id="c4")
    subprocess.run(
        [sys.executable, "-c", "open('n.txt','wb').write(b'n\\n')"],
        cwd=str(root),
        check=True,
    )
    delta.after_and_record()

    assert [r.command_call_id for r in _records(threads)] == ["c4"]


def test_begin_empty_command_call_id_becomes_none(repo):
    root, threads = repo
    ctx = _ctx(root, threads)
    delta = ShellDelta.begin(ctx, command_call_id="")
    subprocess.run(
        [sys.executable, "-c", "open('m.txt','wb').write(b'm\\n')"],
        cwd=str(root),
        check=True,
    )
    delta.after_and_record()

    assert [r.command_call_id for r in _records(threads)] == [None]


def _record(**extra) -> EditRecord:
    return EditRecord(
        path="a.txt",
        turn_id="turn-1",
        ts=1.0,
        call_id=None,
        op="shell",
        hunks="",
        add=0,
        del_=0,
        before_sha256=None,
        after_sha256=None,
        flags=[],
        **extra,
    )


def test_json_round_trip_with_command_call_id():
    obj = _record(command_call_id="c1").to_json_obj()
    assert obj["command_call_id"] == "c1"
    assert EditRecord.from_json_obj(obj).command_call_id == "c1"


def test_json_without_command_call_id_has_no_key():
    obj = _record().to_json_obj()
    assert "command_call_id" not in obj
    assert EditRecord.from_json_obj(obj).command_call_id is None


def test_old_journal_line_reads_as_none():
    line = json.dumps(
        {"path": "a.txt", "turn_id": "t", "call_id": None, "op": "shell"}
    )
    assert EditRecord.from_json_obj(json.loads(line)).command_call_id is None
