"""Оба писателя журнала в одном ходе: fs-тул и шелл-делта.

Один и тот же файл правит сначала тул (``recorder``), потом shell-команда
(``shell_delta``). Проверяем, что записи ложатся в один журнал одним ходом и
что разные формы ``hunks`` и отсутствующий ``before_sha256`` у shell-записи —
это контракт, а не разрыв цепочки целостности.
"""

from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from gc_core.agent_edits.journal import EditsJournal
from gc_core.agent_edits.shell_delta import ShellDelta
from gc_core.config_modules import env_vars
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
    launch_root = tmp_path / "launch"
    launch_root.mkdir()
    threads_dir = tmp_path / "threads"
    threads_dir.mkdir()
    monkeypatch.setattr(env_vars.env, "gc_home", tmp_path / "gc_home")
    _git(launch_root, "init", "-b", "main")
    _git(launch_root, "config", "user.email", "test@example.com")
    _git(launch_root, "config", "user.name", "test")
    _git(launch_root, "config", "core.autocrlf", "false")
    (launch_root / "tracked.txt").write_bytes(b"line1\nline2\n")
    _git(launch_root, "add", "-A")
    _git(launch_root, "commit", "-m", "init")
    return launch_root, threads_dir


def _ctx(launch_root, threads_dir):
    ctx = local_ctx({"launch_root": str(launch_root)})
    ctx.is_tui = True  # режим с записью правок по умолчанию
    ctx.threads_dir = str(threads_dir)
    ctx.system_id = "cli"
    ctx.thread_id = "th1"
    ctx.turn_id = "turn-both"
    return ctx


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


async def test_tool_edit_and_shell_edit_land_in_one_journal_one_turn(repo):
    launch_root, threads_dir = repo
    target = launch_root / "tracked.txt"
    ctx = _ctx(launch_root, threads_dir)

    after_tool = b"line1\nline2x\n"
    tool = WriteToFile(ctx=ctx, call_id="call-tool")
    raw = await tool.acall(path=str(target), content="line1\nline2x\n")
    assert json.loads(raw)["ok"] is True

    delta = ShellDelta.begin(ctx)
    assert delta is not None
    proc = subprocess.run(
        [sys.executable, "-c", "open('tracked.txt','ab').write(b'line3\\n')"],
        cwd=str(launch_root),
        capture_output=True,
    )
    assert proc.returncode == 0, proc.stderr.decode("utf-8", "replace")
    delta.after_and_record()

    after_shell = b"line1\nline2x\nline3\n"
    assert target.read_bytes() == after_shell

    journal_path = edits_journal_path(str(threads_dir), "cli", "th1")
    assert list(threads_dir.glob("edits_*.jsonl")) == [journal_path]
    recs = EditsJournal(journal_path).read_all()
    assert len(recs) == 2
    tool_rec, shell_rec = recs

    # один ход на обе записи
    assert tool_rec.turn_id == "turn-both"
    assert shell_rec.turn_id == "turn-both"
    assert [r.path for r in recs] == ["tracked.txt", "tracked.txt"]

    assert tool_rec.op == "write"
    assert tool_rec.call_id == "call-tool"
    assert tool_rec.before_sha256 == _sha(b"line1\nline2\n")
    assert tool_rec.after_sha256 == _sha(after_tool)

    # контракт shell-записи: call_id нет — правку не делал ни один тул
    assert shell_rec.op == "shell"
    assert shell_rec.call_id is None
    assert shell_rec.after_sha256 == _sha(after_shell)

    # Путь уже известен журналу, значит его снял КАРМАННЫЙ подканал, а он
    # держит точные байты «до» в памяти — и хэш передаёт. Цепочка целостности
    # идёт сквозь шелл-запись, а не рвётся на ней: раньше здесь стоял `None`, и
    # одна команда посреди истории файла глушила признак «правки не от агента»
    # навсегда (пятый раунд ревью). У git-подканала `None` остаётся честным —
    # он видит файл только ПОСЛЕ команды.
    assert shell_rec.before_sha256 == tool_rec.after_sha256

    # две формы unified diff в одном журнале (контракт поля hunks)
    assert tool_rec.hunks.startswith("--- \n+++ \n@@")
    assert shell_rec.hunks.startswith("diff --git")
    assert "+line3" in shell_rec.hunks
