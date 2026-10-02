# packages/core/tests/os_backends/test_remote_shell_backend.py
"""RemoteShellBackend с префиксом ["env"]: «удалённая» сторона = локальный sh."""

import asyncio
import os
import shutil
import subprocess

import pytest
from gc_core.backends.protocol import FsBackend
from gc_core.backends.remote_shell import (
    RemoteShellBackend,
    docker_argv_prefix,
    ssh_argv_prefix,
)

pytestmark = pytest.mark.skipif(
    shutil.which("sh") is None or os.name == "nt", reason="нужен POSIX sh"
)


def _gnu(tool: str) -> bool:
    try:
        out = subprocess.run(
            [tool, "--version"], capture_output=True, text=True, timeout=5
        )
    except Exception:
        return False
    return "GNU" in (out.stdout + out.stderr)


gnu_only = pytest.mark.skipif(
    not (_gnu("stat") and _gnu("find")),
    reason="нужны GNU stat/find (см. docker live test)",
)


@pytest.fixture
def be():
    return RemoteShellBackend(["env"])


def test_is_a_full_backend():
    assert isinstance(RemoteShellBackend(["env"]), FsBackend)
    assert RemoteShellBackend(["env"]).is_sandbox is True
    assert RemoteShellBackend(["env"]).supports_background is True


async def test_aexec_runs_through_prefix_sh_c(be):
    res = await be.aexec("echo $0; echo hi")
    assert res.return_code == 0
    assert res.stdout.endswith("hi\n")


async def test_exec_init_only_on_model_channel():
    be = RemoteShellBackend(["env"], exec_init="export GC_INIT=1")
    assert (await be.aexec("echo ${GC_INIT:-none}")).stdout.strip() == "1"
    assert (
        await be.aexec_raw("echo ${GC_INIT:-none}")
    ).stdout.strip() == "none"


async def test_cwd_and_env_apply(be, tmp_path):
    # `pwd -P`: логический pwd на macOS печатает /var/…, resolve() даёт /private/var/…
    res = await be.aexec(
        "pwd -P; echo $FOO", cwd=str(tmp_path), env={"FOO": "bar baz"}
    )
    assert res.stdout.splitlines() == [str(tmp_path.resolve()), "bar baz"]


async def test_bytes_round_trip_non_utf8(be, tmp_path):
    data = bytes(range(256)) * 300  # > одной строки base64, не UTF-8
    p = str(tmp_path / "deep" / "blob.bin")
    await be.write_bytes(p, data)
    assert await be.read_bytes(p) == data
    assert await be.exists(p)


async def test_write_read_path_with_quote_and_space(be, tmp_path):
    p = str(tmp_path / "it's here" / "a b.txt")
    await be.write(p, "привет\n")
    assert await be.read(p) == "привет\n"
    assert await be.is_file(p) and not await be.is_dir(p)


async def test_append_round_trip(be, tmp_path):
    p = str(tmp_path / "log.txt")
    await be.write(p, "a\n")
    await be.append(p, "b\n")
    assert await be.read(p) == "a\nb\n"


async def test_read_missing_raises_file_not_found(be, tmp_path):
    with pytest.raises(FileNotFoundError):
        await be.read(str(tmp_path / "nope"))


async def test_run_python_feeds_code_on_stdin(be):
    res = await be.arun_python("import sys; print(sys.argv, 2+2)")
    assert res.return_code == 0 and res.stdout.strip().endswith("4")


async def test_timeout_kills_client_and_reports_timed_out(be):
    t0 = asyncio.get_running_loop().time()
    res = await be.aexec("sleep 30", timeout=1)
    assert res.timed_out is True and res.return_code is None
    assert asyncio.get_running_loop().time() - t0 < 10


async def test_aspawn_background_returns_pid(be, tmp_path):
    info = await be.aspawn_background("sleep 5", cwd=str(tmp_path))
    assert isinstance(info["pid"], int) and info["pid"] > 0


@gnu_only
async def test_stat_and_walk_group(be, tmp_path):
    (tmp_path / "d").mkdir()
    (tmp_path / "d" / "x.txt").write_text("12345")
    assert await be.getsize(str(tmp_path / "d" / "x.txt")) == 5
    assert await be.listdir(str(tmp_path)) == [("d", True)]
    entries = await be.walk_entries(str(tmp_path))
    assert {(e.rel, e.is_dir) for e in entries} == {
        ("d", True),
        ("d/x.txt", False),
    }


async def test_aprime_default_cwd_uses_configured_cwd_first(tmp_path):
    be = RemoteShellBackend(["env"], cwd=str(tmp_path))
    assert be.default_cwd() == str(tmp_path)


async def test_aprime_default_cwd_asks_pwd_when_unset(be):
    assert (await be.aprime_default_cwd()) == os.getcwd()


# --- префиксы ---
def test_docker_prefix():
    assert docker_argv_prefix({"docker": {"name": "box"}}) == [
        "docker",
        "exec",
        "-i",
        "box",
    ]
    assert docker_argv_prefix(
        {"docker": {"name": "box", "user": "me", "binary": "podman"}}
    ) == [
        "podman",
        "exec",
        "-i",
        "-u",
        "me",
        "box",
    ]


def test_ssh_prefix_batch_mode_and_control_master(tmp_path):
    pre = ssh_argv_prefix(
        {
            "ssh": {
                "host": "h",
                "port": 2222,
                "user": "u",
                "key_path": "~/.ssh/k",
                "options": ["StrictHostKeyChecking=accept-new"],
                "control_master": True,
            }
        },
        control_dir=tmp_path,
    )
    assert pre[:2] == ["ssh", "-T"]
    assert (
        "BatchMode=yes" in pre
        and "-p" in pre
        and pre[pre.index("-p") + 1] == "2222"
    )
    assert pre[pre.index("-i") + 1] == os.path.expanduser("~/.ssh/k")
    assert any(
        o.startswith("ControlPath=") and str(tmp_path) in o for o in pre
    )
    assert pre[-2:] == ["u@h", "--"]


def test_ssh_prefix_without_user_key_or_control(tmp_path):
    pre = ssh_argv_prefix(
        {"ssh": {"host": "h", "control_master": False}}, control_dir=tmp_path
    )
    assert "-i" not in pre and not any("ControlMaster" in o for o in pre)
    assert pre[-2:] == ["h", "--"]


# --- транспорт, который склеивает argv (так делает ssh) ---
# Шим ведёт себя как OpenSSH: всё после хоста склеивается пробелами и уходит
# одной строкой в шелл той стороны.
_SSH_LIKE = ["sh", "-c", 'exec sh -c "$*"', "fake-ssh"]


async def test_joining_transport_keeps_cwd_and_writes(tmp_path):
    d = tmp_path / "rv dir"
    d.mkdir()
    be = RemoteShellBackend(_SSH_LIKE, cwd=str(d), joins_argv=True)
    res = await be.aexec("echo hi; pwd -P")
    assert res.stdout.splitlines() == ["hi", str(d.resolve())]
    p = str(d / "it's" / "a b.txt")
    await be.write(p, "x")
    assert await be.read(p) == "x"


async def test_fs_ops_have_a_timeout():
    # Зависший транспорт (пауза контейнера, мёртвый хост) не вешает ФС-операции.
    be = RemoteShellBackend(
        ["sh", "-c", "sleep 30", "hang"], fs_op_timeout_sec=1
    )
    t0 = asyncio.get_running_loop().time()
    assert await be.exists("/x") is False
    with pytest.raises(OSError):
        await be.write("/x", "y")
    with pytest.raises(OSError):
        await be.read_bytes("/x")
    assert asyncio.get_running_loop().time() - t0 < 15


def test_ssh_prefix_has_connect_timeout_and_keepalive(tmp_path):
    pre = ssh_argv_prefix({"ssh": {"host": "h"}}, control_dir=tmp_path)
    assert "ConnectTimeout=10" in pre and "ServerAliveInterval=15" in pre
