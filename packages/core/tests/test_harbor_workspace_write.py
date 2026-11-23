import asyncio
import base64
import shlex
import subprocess
from contextlib import contextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest
from gc_core.backends.factory import resolve_os_backend
from gc_core.backends.harbor import _BASHRC_EXEC_INIT, _PUBLISH_WORKSPACE_WRITE
from gc_core.backends.harbor_workspace import HarborWorkspace
from gc_core.backends.local import LocalFsBackend
from gc_testkit.runtime_probe import probe_stdout
from gc_testkit.settings import make_settings


class TrialEnvironment:
    def __init__(self, *, preflight_error="", temp_root="/tmp"):
        self.preflight_error = preflight_error
        self.temp_root = temp_root
        self.calls = []
        self.uploads = []
        self.scoped_env = {}

    @contextmanager
    def scoped_exec_env(self, env):
        previous = self.scoped_env
        self.scoped_env = previous | env
        try:
            yield
        finally:
            self.scoped_env = previous

    async def exec(self, command, **kwargs):
        kwargs["env"] = kwargs.get("env", {}) | self.scoped_env
        self.calls.append((command, kwargs))
        stdout = probe_stdout(command) or ""
        if "pwd -P" in command:
            stdout = "\0".join(
                (
                    "/work",
                    self.temp_root,
                    "/opt/image-bin:/usr/bin:/bin",
                    "/opt/image-lib",
                    "",
                    "",
                    "",
                )
            )
        if "if test -w" in command:
            protected = (
                command.startswith("exec ")
                and "locked file" in shlex.split(command)[-1]
            )
            stdout = "0\n" if protected else "1\n"
        if "/bin/true" in command and self.preflight_error:
            return SimpleNamespace(
                stdout=self.preflight_error, stderr="", return_code=1
            )
        return SimpleNamespace(stdout=stdout, stderr="", return_code=0)

    async def upload_file(self, source, target):
        self.uploads.append((Path(source).read_bytes(), target))


def test_workspace_overwrites_existing_inode_when_namespace_chown_fails(
    tmp_path,
):
    if not Path("/bin/bash").is_file():
        pytest.skip("Bash is required to run the workspace write script")
    target = tmp_path / "target"
    target.write_text("old", encoding="utf-8")
    original_inode = target.stat().st_ino
    payload = tmp_path / "payload"
    payload.write_text("new", encoding="utf-8")
    variables = {
        "target": target,
        "parent": tmp_path,
        "adjacent": tmp_path / "adjacent",
        "fallback": tmp_path / "fallback",
        "payload": payload,
        "operation": "write",
    }
    assignments = "".join(
        f"{name}={shlex.quote(str(value))}\n"
        for name, value in variables.items()
    )
    namespace = """
stat() {
    case "$2" in
        '%u:%g %a') printf '%s\\n' '10001:10002 666' ;;
        '%u:%g') printf '%s\\n' '0:0' ;;
        *) return 1 ;;
    esac
}
chown() { return 1; }
id() { printf '%s\\n' 0; }
"""
    result = subprocess.run(
        [
            "/bin/bash",
            "-c",
            assignments + namespace + _PUBLISH_WORKSPACE_WRITE,
        ],
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert target.read_text(encoding="utf-8") == "new"
    assert target.stat().st_ino == original_inode


def workspace_backend(environment):
    settings = make_settings(
        {
            "os_backend": "harbor-workspace",
            "agent": {"harbor_exec_init": "bashrc"},
            "security": {"protected_paths": {"extra": ["/work/locked file"]}},
        }
    )
    return resolve_os_backend(
        settings, environment=environment, loop=asyncio.get_running_loop()
    )


async def test_workspace_exec_keeps_image_path_and_applies_env_inside_bwrap():
    env = TrialEnvironment()
    backend = workspace_backend(env)
    phase_env = {"PATH": "/work/bin:/usr/bin", "BASH_ENV": "/work/phase.sh"}
    with env.scoped_exec_env(phase_env):
        await backend.aprepare_workspace("/work")
        await backend.aexec(
            "image-tool; bashrc-tool",
            cwd="/work/subdir",
            env={"BASH_ENV": "/work/rc"},
        )
        assert env.scoped_env == phase_env
    command, kwargs = env.calls[-1]
    argv = shlex.split(command)

    assert argv[:2] == ["exec", "/usr/bin/bwrap"]
    assert argv[argv.index("--chdir") + 1] == "/work/subdir"
    assert argv[argv.index("--bind") :][:6] == [
        "--bind",
        "/tmp",
        "/tmp",
        "--bind",
        "/work",
        "/work",
    ]
    assert argv[argv.index("--remount-ro") + 1] == "/proc"
    assert argv[-1].find(".bashrc") < argv[-1].find("image-tool; bashrc-tool")
    assert "/opt/image-bin:/usr/bin:/bin" in argv
    assert "/opt/image-lib" in argv
    assert "/work/rc" in argv
    assert env.calls[0][1]["env"]["BASH_ENV"] == ""
    assert "/work/bin" not in kwargs["env"]["PATH"]
    assert kwargs["env"]["BASH_ENV"] == ""
    assert kwargs["env"]["LD_LIBRARY_PATH"] == ""
    assert not backend.supports_background


async def test_workspace_bashrc_honors_interactive_guard(
    tmp_path, monkeypatch
):
    if not Path("/bin/bash").is_file():
        pytest.skip("Bash is required to run the generated shell bootstrap")
    image_bin = tmp_path / "image-bin"
    rc_bin = tmp_path / "rc-bin"
    for directory, name in ((image_bin, "image-tool"), (rc_bin, "rc-tool")):
        directory.mkdir()
        tool = directory / name
        tool.write_text(
            f"#!/bin/sh\nprintf '%s\\n' '{name}'\n", encoding="utf-8"
        )
        tool.chmod(0o755)
    (tmp_path / ".bashrc").write_text(
        "case $- in *i*) ;; *) return ;; esac\n"
        f'export PATH={shlex.quote(str(rc_bin))}:"$PATH"\n'
        "printf 'startup noise\\n' >&2\n"
        "set --\n",
        encoding="utf-8",
    )
    profile = tmp_path / "profile"
    profile.write_text("export PATH=/bin\n", encoding="utf-8")
    monkeypatch.setattr(
        "gc_core.backends.harbor._BASHRC_EXEC_INIT",
        _BASHRC_EXEC_INIT.replace(
            ". /etc/profile", f". {shlex.quote(str(profile))}"
        ),
    )
    env = TrialEnvironment()
    backend = workspace_backend(env)
    await backend.aprepare_workspace("/work")
    await backend.aexec(
        "image-tool && rc-tool; printf 'diagnostic\\n' >&2; exit 7"
    )
    argv = shlex.split(env.calls[-1][0])
    result = subprocess.run(
        argv[argv.index("--") + 1 :],
        env={"PATH": f"{image_bin}:/bin", "HOME": str(tmp_path)},
        cwd=tmp_path,
        capture_output=True,
        check=False,
        text=True,
    )

    assert result.returncode == 7
    assert result.stdout == "image-tool\nrc-tool\n"
    assert result.stderr == "diagnostic\n"


@pytest.mark.parametrize(
    "config",
    [
        {"os_backend.kind": "harbor-workspace"},
        {
            "os_backend.kind": "harbor",
            "os_backend.harbor_workspace_write": True,
        },
    ],
)
def test_workspace_write_rejects_a_different_effective_backend(config):
    settings = make_settings(config).with_runtime(os_backend="local")

    with pytest.raises(ValueError, match="requires the Harbor backend"):
        resolve_os_backend(settings)


def test_workspace_write_rejects_an_explicit_backend_override():
    settings = make_settings({"os_backend": "harbor-workspace"})

    with pytest.raises(ValueError, match="requires the Harbor backend"):
        resolve_os_backend(settings, override=LocalFsBackend())


async def test_workspace_stages_and_publishes_file_mutations_inside_bwrap():
    env = TrialEnvironment()
    backend = workspace_backend(env)
    await backend.aprepare_workspace("/work")
    env.calls.clear()

    data = b"a\x00b\xff" * 30_000
    await backend.write_bytes("/work/file.txt", data)
    await backend.copy("/work/file.txt", "/work/backup.txt")
    await backend.makedirs("/work/dir")

    assert env.uploads == []
    scoped = [shlex.split(c) for c, _ in env.calls if c.startswith("exec ")]
    bodies = [argv[-1] for argv in scoped]
    chunks = [
        shlex.split(body)[2]
        for body in bodies
        if body.startswith("printf '%s' ")
    ]
    assert len(chunks) > 1
    assert b"".join(base64.b64decode(chunk) for chunk in chunks) == data
    assert "cp /work/file.txt /work/backup.txt" in bodies
    assert "mkdir -p /work/dir" in bodies
    for argv in scoped:
        assert argv[:2] == ["exec", "/usr/bin/bwrap"]
        assert argv[-1].strip()
        assert argv.index("--bind") < argv.index("/work/locked file")
        assert argv.count("/work/locked file") == 2
        assert "/work/.git" in argv
        assert argv[argv.index("--bind") :][:3] == ["--bind", "/tmp", "/tmp"]
        assert "/opt/image-lib" not in argv


@pytest.mark.parametrize("lost_reply", ["prepare", "publish"])
async def test_workspace_cleans_both_staging_paths_after_a_lost_reply(
    lost_reply,
):
    class LostReplyEnvironment(TrialEnvironment):
        async def exec(self, command, **kwargs):
            result = await super().exec(command, **kwargs)
            if command.startswith("exec "):
                body = shlex.split(command)[-1]
                starts_stage = body.startswith("mkdir -m 700")
                publishes_file = "mv -fT" in body
                if (lost_reply == "prepare" and starts_stage) or (
                    lost_reply == "publish" and publishes_file
                ):
                    raise TimeoutError
            return result

    env = LostReplyEnvironment()
    backend = workspace_backend(env)
    await backend.aprepare_workspace("/work")
    with pytest.raises(OSError, match="timed out"):
        await backend.write_bytes("/work/subdir/file.txt", b"content")

    cleanup = shlex.split(env.calls[-1][0])[-1]
    assert "fallback=/work/.gc-write-" in cleanup
    assert "adjacent=/work/subdir/.gc-write-" in cleanup
    assert 'in "$fallback" "$adjacent"' in cleanup
    with pytest.raises(
        RuntimeError, match="completion could not be confirmed"
    ):
        await backend.arelease_workspace()


async def test_workspace_preflight_fails_closed_if_bwrap_cannot_run():
    env = TrialEnvironment(preflight_error="bwrap: Operation not permitted")
    backend = workspace_backend(env)

    with pytest.raises(RuntimeError, match="bwrap: Operation not permitted"):
        await backend.aprepare_workspace("/work")
    assert "Cannot release Harbor metadata reservation" in env.calls[-1][0]
    with pytest.raises(RuntimeError, match="workspace"):
        await backend.aexec("echo must not run")
    assert all("must not run" not in command for command, _ in env.calls)


async def test_workspace_reports_writability_in_the_write_namespace():
    backend = workspace_backend(TrialEnvironment())
    await backend.aprepare_workspace("/work")

    assert await backend.writable("/work/open.txt")
    assert not await backend.writable("/work/locked file")


async def test_workspace_reserves_missing_metadata_until_trial_cleanup():
    env = TrialEnvironment()
    backend = workspace_backend(env)
    await backend.aprepare_workspace("/work")
    await backend.aexec("pwd")
    argv = shlex.split(env.calls[-1][0])
    for entry in (".git", ".gc", ".agents", ".codex", "AGENTS.md"):
        path = f"/work/{entry}"
        assert argv[argv.index(path) - 1 :][:3] == ["--ro-bind", path, path]

    await backend.arelease_workspace()
    cleanup = env.calls[-1][0]
    reservation = next(c for c, _ in env.calls if "validate_mount_path" in c)
    lease_assignment = reservation.splitlines()[0]
    assert cleanup.splitlines()[0] == lease_assignment
    lease = lease_assignment.removeprefix("lease=")
    assert argv[argv.index(lease) - 1 :][:3] == ["--ro-bind", lease, lease]
    assert "Cannot release Harbor metadata reservation" in cleanup


async def test_lost_reservation_reply_still_cleans_the_trial_lease():
    class LostReservationEnvironment(TrialEnvironment):
        async def exec(self, command, **kwargs):
            result = await super().exec(command, **kwargs)
            if "validate_mount_path" in command:
                raise TimeoutError
            return result

    env = LostReservationEnvironment()
    backend = workspace_backend(env)
    with pytest.raises(TimeoutError):
        await backend.aprepare_workspace("/work")

    reservation = env.calls[-2][0]
    cleanup = env.calls[-1][0]
    assert "validate_mount_path" in reservation
    assert "Cannot release Harbor metadata reservation" in cleanup
    assert reservation.splitlines()[0] == cleanup.splitlines()[0]


@pytest.mark.parametrize(
    ("root", "protected", "binds"),
    [
        ("/work", "/work/src/private.txt", ["/tmp", "/work", "/work/src"]),
        (
            "/tmp/task/work",
            "/tmp/task/locked",
            ["/tmp", "/tmp/task", "/tmp/task/work"],
        ),
    ],
)
def test_workspace_keeps_writable_ancestors_mounted(root, protected, binds):
    workspace = HarborWorkspace(root, (), (protected,), "/tmp/reservation")
    argv = shlex.split(workspace.wrap("pwd"))
    assert [
        argv[i + 1] for i, arg in enumerate(argv) if arg == "--bind"
    ] == binds
    assert argv[argv.index(protected) - 1 :][:3] == [
        "--ro-bind",
        protected,
        protected,
    ]


async def test_workspace_rejects_a_tmp_alias_to_the_container_root():
    backend = workspace_backend(TrialEnvironment(temp_root="/"))
    with pytest.raises(RuntimeError, match="/tmp"):
        await backend.aprepare_workspace("/work")
