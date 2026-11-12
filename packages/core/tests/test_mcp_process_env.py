"""Окружение MCP-процесса: каталог команды, заданной путём, — первым в PATH.

`npx` из Node агента — скрипт с `#!/usr/bin/env node`: интерпретатор он ищет
через PATH. Без каталога команды в PATH он берёт системную Node (старую —
падает npm) или никакую (`env: node: No such file or directory`).
"""

import os
import subprocess

import pytest

from gc_core.mcp_runtime import env as mcp_env
from gc_core.mcp_runtime.env import mcp_process_env

posix_only = pytest.mark.skipif(os.name == "nt", reason="shebang — POSIX")
pytestmark = pytest.mark.usefixtures("bare_path")


@pytest.fixture
def bare_path(monkeypatch):
    """PATH агента без mcp-env: проверяется только правило каталога команды."""
    monkeypatch.setattr(mcp_env, "_effective_bin_dir", lambda: None)
    monkeypatch.setenv("PATH", os.pathsep.join(["/usr/bin", "/bin"]))


def _agent_node(tmp_path):
    """Каталог как у Node агента: `node` и скрипт на `#!/usr/bin/env node`."""
    bin_dir = tmp_path / "node" / "bin"
    bin_dir.mkdir(parents=True)
    node = bin_dir / "node"
    node.write_text("#!/bin/sh\necho agent-node\n", encoding="utf-8")
    node.chmod(0o755)
    tool = bin_dir / "npx"
    tool.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    tool.chmod(0o755)
    return bin_dir, tool


def test_command_dir_goes_first(tmp_path):
    bin_dir, tool = _agent_node(tmp_path)
    path = mcp_process_env(str(tool))["PATH"].split(os.pathsep)
    assert path == [str(bin_dir), "/usr/bin", "/bin"]


def test_dir_already_in_path_is_not_moved():
    # Команда из системного каталога не переставляет его вперёд: иначе
    # ребёнок увидел бы системные версии инструментов раньше пользовательских.
    env = mcp_process_env("/bin/sh")
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])


@posix_only
def test_dir_reached_through_a_link_counts_as_present(tmp_path, monkeypatch):
    """`/bin` — ссылка на `/usr/bin` (Arch, Fedora): команда `/bin/bash` не
    должна ставить системный каталог впереди пользовательских."""
    real = tmp_path / "usr-bin"
    real.mkdir()
    (tmp_path / "bin").symlink_to(real)
    monkeypatch.setenv("PATH", os.pathsep.join(["/opt/user", str(real)]))
    env = mcp_process_env(str(tmp_path / "bin" / "bash"))
    assert env["PATH"] == os.pathsep.join(["/opt/user", str(real)])


def test_empty_path_entries_are_kept(tmp_path, monkeypatch):
    # Пустой элемент PATH на POSIX — текущий каталог: не теряется.
    bin_dir, tool = _agent_node(tmp_path)
    monkeypatch.setenv("PATH", os.pathsep.join(["", "/usr/bin", ""]))
    env = mcp_process_env(str(tool))
    assert env["PATH"] == os.pathsep.join([str(bin_dir), "", "/usr/bin", ""])


def test_relative_path_entries_do_not_count(tmp_path, monkeypatch):
    # «.» в PATH ребёнок разрешит от своего cwd, а не от cwd агента: такой
    # элемент не может подтвердить, что каталог команды уже есть.
    bin_dir, tool = _agent_node(tmp_path)
    monkeypatch.chdir(bin_dir)
    monkeypatch.setenv("PATH", os.pathsep.join([".", "", "/usr/bin"]))
    env = mcp_process_env(str(tool))
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)


@posix_only
def test_linked_command_brings_the_dir_of_the_link(tmp_path):
    """`bin/npx` в Node — ссылка на скрипт npm в другом каталоге: в PATH
    нужен каталог ссылки, где лежит `node`, а не каталог цели."""
    bin_dir, _tool = _agent_node(tmp_path)
    target = tmp_path / "lib" / "npx-cli.js"
    target.parent.mkdir()
    target.write_text("#!/usr/bin/env node\n", encoding="utf-8")
    target.chmod(0o755)
    link = bin_dir / "npx-link"
    link.symlink_to(target)
    env = mcp_process_env(str(link))
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)
    out = subprocess.run(
        [str(link)], env=env, capture_output=True, text=True, check=True
    ).stdout
    assert out == "agent-node\n"


def test_bare_command_adds_nothing():
    env = mcp_process_env("npx")
    assert env["PATH"] == os.pathsep.join(["/usr/bin", "/bin"])


def test_server_env_path_wins(tmp_path):
    _bin_dir, tool = _agent_node(tmp_path)
    env = mcp_process_env(str(tool), {"PATH": "/custom", "X": 1})
    assert env["PATH"] == "/custom"
    assert env["X"] == "1"


@posix_only
def test_script_finds_its_own_interpreter(tmp_path):
    """Тот самый механизм: скрипт на `#!/usr/bin/env node` запускается Node
    из своего каталога, а не той, что окажется (или не окажется) в системе."""
    _bin_dir, tool = _agent_node(tmp_path)
    out = subprocess.run(
        [str(tool)],
        env=mcp_process_env(str(tool)),
        capture_output=True,
        text=True,
        check=True,
    ).stdout
    assert out == "agent-node\n"


@posix_only
def test_every_stdio_launcher_uses_it(tmp_path):
    """Одноразовый обмен, долгоживущая сессия и SDK-клиент стартуют сервер с
    одним окружением: расхождение — тот же сервер с разной Node."""
    from gc_core.tools.connect_mcp import persistent_stdio, stream_client
    from gc_core.tools.connect_mcp.transport import _stdio_command

    bin_dir, tool = _agent_node(tmp_path)
    cfg = {"command": str(tool), "args": []}

    _cmd, _cwd, env = _stdio_command(cfg)
    assert env["PATH"].split(os.pathsep)[0] == str(bin_dir)

    params = stream_client._stdio_server_params(cfg)
    assert params.env["PATH"].split(os.pathsep)[0] == str(bin_dir)

    proc = persistent_stdio._spawn_detached_stdio_process(cfg)
    try:
        assert proc.stdout.readline() == "agent-node\n"
    finally:
        proc.wait(timeout=10)
        proc.stdin.close()
        proc.stdout.close()
