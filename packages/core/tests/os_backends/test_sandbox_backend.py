# packages/core/tests/os_backends/test_sandbox_backend.py
"""SandboxBackend: файлы на хосте, shell-команды модели — в bwrap / sandbox-exec."""

import os
import shutil
import sys

import pytest
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.sandbox import (
    SandboxBackend,
    build_sandbox_wrapper,
    bwrap_wrapper,
    seatbelt_wrapper,
)

HAVE_TOOL = (sys.platform == "darwin" and shutil.which("sandbox-exec")) or (
    sys.platform.startswith("linux") and shutil.which("bwrap")
)
live = pytest.mark.skipif(
    not HAVE_TOOL, reason="нужен bwrap (Linux) или sandbox-exec (macOS)"
)


def _be(root, **kw):
    return SandboxBackend(
        wrapper=build_sandbox_wrapper(str(root), {"sandbox": kw}),
        root=str(root),
    )


@pytest.fixture
def outside(tmp_path):
    """Путь ВНЕ root и вне TMPDIR: pytest'овский tmp_path лежит в системном temp,
    который профиль sandbox-exec разрешает (иначе внутри не работает tempfile).
    Поэтому «снаружи» — домашний каталог; на Linux он ro-bind, на macOS не в allow."""
    import uuid
    from pathlib import Path

    p = Path.home() / f".gc-sandbox-test-{uuid.uuid4().hex}.txt"
    yield p
    p.unlink(missing_ok=True)


def test_is_local_not_sandbox_flag(tmp_path):
    be = SandboxBackend(wrapper=lambda cwd: [], root=str(tmp_path))
    assert isinstance(be, LocalFsBackend)
    assert (
        be.is_sandbox is False
    )  # файлы настоящие → guards работают как для local


def test_bwrap_argv_shape(tmp_path):
    argv = bwrap_wrapper(
        str(tmp_path), network=True, extra_ro=["/opt/x"], home_to_root=False
    )(str(tmp_path))
    assert (
        argv[0] == "bwrap"
        and "--unshare-all" in argv
        and "--share-net" in argv
    )
    assert "--die-with-parent" in argv
    i = argv.index("--bind")
    assert argv[i + 1] == argv[i + 2] == str(tmp_path)
    assert argv[argv.index("--chdir") + 1] == str(tmp_path)
    assert "--ro-bind" in argv and "/usr" in argv


def test_bwrap_no_network_drops_share_net(tmp_path):
    argv = bwrap_wrapper(
        str(tmp_path), network=False, extra_ro=[], home_to_root=False
    )(None)
    assert "--share-net" not in argv


def test_seatbelt_profile_mentions_root_and_denies_network(tmp_path):
    argv = seatbelt_wrapper(str(tmp_path), network=False)(None)
    assert argv[:2] == ["sandbox-exec", "-p"]
    profile = argv[2]
    assert "(deny file-write*)" in profile
    assert f'(subpath "{os.path.realpath(tmp_path)}")' in profile
    assert "(deny network*)" in profile


def test_build_wrapper_fails_closed_without_tool(tmp_path, monkeypatch):
    monkeypatch.setattr(shutil, "which", lambda name: None)
    with pytest.raises(ValueError, match="sandbox"):
        build_sandbox_wrapper(str(tmp_path), {"sandbox": {}})


def test_build_wrapper_rejects_windows(tmp_path, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    with pytest.raises(ValueError, match="Windows"):
        build_sandbox_wrapper(str(tmp_path), {"sandbox": {}})


@live
async def test_write_inside_root_allowed(tmp_path):
    be = _be(tmp_path)
    res = await be.aexec("echo ok > inside.txt", cwd=str(tmp_path))
    assert (
        res.return_code == 0
        and (tmp_path / "inside.txt").read_text() == "ok\n"
    )


@live
async def test_write_outside_root_denied(tmp_path, outside):
    root = tmp_path / "root"
    root.mkdir()
    be = _be(root)
    res = await be.aexec(f"echo x > {outside}", cwd=str(root))
    assert res.return_code != 0 and not outside.exists()


@live
async def test_symlink_escape_denied(tmp_path, outside):
    root = tmp_path / "root"
    root.mkdir()
    (root / "link").symlink_to(outside.parent)
    be = _be(root)
    res = await be.aexec(f"echo x > link/{outside.name}", cwd=str(root))
    assert res.return_code != 0 and not outside.exists()


@live
async def test_no_network_blocks_sockets(tmp_path):
    be = _be(tmp_path, network=False)
    code = (
        "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
    )
    res = await be.arun_python(code, cwd=str(tmp_path))
    assert res.return_code != 0


@live
async def test_run_python_and_argv_go_through_wrapper(tmp_path):
    be = _be(tmp_path)
    assert (
        await be.arun_python("print(6*7)", cwd=str(tmp_path))
    ).stdout.strip() == "42"
    assert (
        await be.aexec_argv(["echo", "a b"], cwd=str(tmp_path))
    ).stdout == "a b\n"


@live
async def test_background_is_wrapped_too(tmp_path, outside):
    root = tmp_path / "root"
    root.mkdir()
    be = _be(root)
    info = await be.aspawn_background(
        f"sleep 1; echo x > {outside}", cwd=str(root)
    )
    assert info["pid"] > 0
    import asyncio

    await asyncio.sleep(2)
    assert not outside.exists()


def test_fs_ops_stay_local(tmp_path):
    be = SandboxBackend(
        wrapper=lambda cwd: ["definitely-not-a-binary"], root=str(tmp_path)
    )
    import asyncio

    asyncio.run(be.write(str(tmp_path / "f.txt"), "hi"))
    assert (
        tmp_path / "f.txt"
    ).read_text() == "hi"  # запись не идёт через обёртку


def test_tracking_wraps_sandbox_like_local(tmp_path):
    from gc_core.agent_edits.tracking import (
        TrackingFsBackend,
        wrap_local_for_tracking,
    )

    be = SandboxBackend(wrapper=lambda cwd: [], root=str(tmp_path))
    wrapped = wrap_local_for_tracking(be, object())
    assert isinstance(wrapped, TrackingFsBackend)
    assert wrapped.isolates_exec is True


async def test_background_spawn_outlives_parent_thread(tmp_path, monkeypatch):
    # PDEATHSIG у bwrap срабатывает на выход ПОТОКА-родителя: фоновый сервис,
    # запущенный из рабочего потока хода, умирал бы вместе с ходом.
    seen = {}

    async def fake_spawn(self, command, *, cwd=None, env_full=None):
        seen["cmd"] = command
        seen["env"] = env_full
        return {"pid": 1, "exit_code": None}

    monkeypatch.setattr(LocalFsBackend, "_aspawn_detached", fake_spawn)
    monkeypatch.setenv("GC_SB_BG_SECRET", "leak")
    wrap = bwrap_wrapper(
        str(tmp_path), network=True, extra_ro=[], home_to_root=False
    )
    be = SandboxBackend(wrapper=wrap, root=str(tmp_path))
    await be.aspawn_background("sleep 1", cwd=str(tmp_path))
    assert (
        seen["cmd"].startswith("bwrap ")
        and "--die-with-parent" not in seen["cmd"]
    )
    # фон получает тот же allowlist, что передний план, а не os.environ целиком
    assert seen["env"] is not None and "GC_SB_BG_SECRET" not in seen["env"]
    assert "PATH" in seen["env"]


def test_default_cwd_is_root_when_no_auto_workdir(tmp_path):
    # workdir=null и launch_root ≠ реальный cwd процесса: команды по умолчанию
    # обязаны идти в корень песочницы, а не в os.getcwd() вне него.
    be = SandboxBackend(wrapper=lambda cwd: [], root=str(tmp_path))
    assert be.default_cwd() == str(tmp_path)


def test_build_wrapper_fails_closed_when_bwrap_cannot_unshare(
    tmp_path, monkeypatch
):
    # bwrap есть, но user namespaces выключены (CI, контейнер): без пробы
    # команды падали бы в рантайме с криптичной ошибкой вместо отказа на старте.
    import subprocess

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    fake = subprocess.CompletedProcess(
        args=[],
        returncode=1,
        stdout="",
        stderr="bwrap: No permissions to create new namespace",
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: fake)
    with pytest.raises(ValueError, match="namespace"):
        build_sandbox_wrapper(str(tmp_path), {"sandbox": {}})


def test_build_wrapper_accepts_working_bwrap(tmp_path, monkeypatch):
    import subprocess

    monkeypatch.setattr(sys, "platform", "linux")
    monkeypatch.setattr(shutil, "which", lambda name: "/usr/bin/bwrap")
    ok = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="", stderr=""
    )
    monkeypatch.setattr(subprocess, "run", lambda *a, **k: ok)
    assert (
        build_sandbox_wrapper(str(tmp_path), {"sandbox": {}})(None)[0]
        == "bwrap"
    )


def test_bwrap_extra_ro_inside_root_stays_read_only(tmp_path):
    # Поздний `--bind root` перекрывает ранний ro-bind пути внутри корня:
    # extra_ro внутри launch_root молча становился записываемым.
    inside = tmp_path / "ro"
    inside.mkdir()
    argv = bwrap_wrapper(
        str(tmp_path), network=True, extra_ro=[str(inside)], home_to_root=False
    )(str(tmp_path))
    root_bind = argv.index("--bind")
    ro_inside = [
        i
        for i, a in enumerate(argv)
        if a == "--ro-bind" and argv[i + 1] == str(inside.resolve())
    ]
    assert ro_inside and ro_inside[0] > root_bind
    # повторов одного ro-bind нет
    pairs = [argv[i + 1] for i, a in enumerate(argv) if a == "--ro-bind"]
    assert len(pairs) == len(set(pairs))


@pytest.mark.skipif(
    not HAVE_TOOL, reason="нужен bwrap (Linux) или sandbox-exec (macOS)"
)
async def test_shell_does_not_inherit_agent_process_env(tmp_path, monkeypatch):
    # Shell модели получал os.environ агента целиком (токены, ключи), хотя
    # run_python чистил окружение. Теперь оба — один allowlist.
    monkeypatch.setenv("GC_SB_TEST_SECRET", "leak")
    root = str(tmp_path)
    be = SandboxBackend(
        wrapper=build_sandbox_wrapper(root, {"sandbox": {"network": True}}),
        root=root,
        auto_workdir=None,
        exec_output_capture_max_chars=5000,
    )
    res = await be.aexec(
        'echo "s=${GC_SB_TEST_SECRET:-absent} p=${PATH:+set}"', timeout=20
    )
    assert res.stdout.strip() == "s=absent p=set", res.stdout + res.stderr
    res = await be.aexec_argv(
        ["sh", "-c", 'echo "${GC_SB_TEST_SECRET:-absent}/${EXPLICIT:-none}"'],
        env={"EXPLICIT": "ok"},
        timeout=20,
    )
    assert res.stdout.strip() == "absent/ok"


async def test_exec_refreshes_root_mtime_for_janitor(tmp_path):
    import time

    wrap = bwrap_wrapper(
        str(tmp_path), network=True, extra_ro=[], home_to_root=False
    )
    be = SandboxBackend(wrapper=wrap, root=str(tmp_path))
    old = time.time() - 3 * 24 * 3600
    os.utime(tmp_path, (old, old))
    be._touch_root()
    assert os.path.getmtime(tmp_path) > old + 24 * 3600
