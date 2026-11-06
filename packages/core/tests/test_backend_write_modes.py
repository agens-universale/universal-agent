"""Real filesystem regressions for file mode preservation on overwrite."""

import asyncio
import os
import shutil
import stat
import subprocess
import sys
import threading
from pathlib import Path

import pytest
from gc_core.backends.harbor import HarborBackend
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult
from gc_testkit.runtime_probe import probe_stdout


def _mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_local_overwrite_preserves_executable_mode(tmp_path):
    path = tmp_path / "script.sh"
    path.write_bytes(b"#!/bin/sh\necho old\n")
    path.chmod(0o755)

    await LocalFsBackend().write_bytes(str(path), b"#!/bin/sh\necho new\n")

    assert _mode(path) == 0o755
    assert (
        subprocess.run([str(path)], capture_output=True, check=True).stdout
        == b"new\n"
    )


class _LocalHarborEnvironment:
    """Run Harbor shell commands locally while upload replaces its target inode."""

    def __init__(self):
        self.fail_write = False
        self.fail_replace = False
        self.fail_prepare = False
        self.malformed_stat = False
        self.uploaded = []

    async def exec(self, command, **kwargs):
        probe = probe_stdout(command)
        if probe is not None:
            return ExecResult(stdout=probe, stderr="", return_code=0)
        if self.fail_prepare and 'mkdir -m 700 -- "$stage"' in command:
            return ExecResult(
                stdout="", stderr="Permission denied", return_code=1
            )
        if sys.platform == "darwin":
            command = (
                command.replace("stat -c '%u:%g %a'", "stat -f '%u:%g %Lp'")
                .replace("stat -c '%u:%g'", "stat -f '%u:%g'")
                .replace("mv -fT --", "mv -fh --")
            )
        if self.malformed_stat:
            command = "stat() { return 0; }\n" + command
        if self.fail_write:
            command = (
                "cat() { printf partial; echo 'write failed' >&2; return 1; }\n"
                + command
            )
        if self.fail_replace:
            command = (
                "mv() { echo 'replace failed' >&2; return 1; }\n" + command
            )
        result = subprocess.run(
            command, shell=True, capture_output=True, text=True
        )
        return ExecResult(
            stdout=result.stdout,
            stderr=result.stderr,
            return_code=result.returncode,
        )

    async def upload_file(self, src, dst):
        self.uploaded.append(dst)
        target = Path(dst)
        staged = target.with_name(target.name + ".upload")
        shutil.copyfile(src, staged)
        staged.chmod(0o644)
        os.replace(staged, target)


@pytest.fixture
def harbor_loop():
    loop = asyncio.new_event_loop()
    thread = threading.Thread(target=loop.run_forever, daemon=True)
    thread.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    thread.join(timeout=2)
    loop.close()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_harbor_overwrite_preserves_executable_mode(
    tmp_path, harbor_loop
):
    path = tmp_path / "script.sh"
    path.write_bytes(b"#!/bin/sh\necho old\n")
    path.chmod(0o755)
    owner = (path.stat().st_uid, path.stat().st_gid)
    backend = HarborBackend(_LocalHarborEnvironment(), harbor_loop)

    await backend.write_bytes(str(path), b"#!/bin/sh\necho new\n")

    assert _mode(path) == 0o755
    assert (path.stat().st_uid, path.stat().st_gid) == owner
    assert (
        subprocess.run([str(path)], capture_output=True, check=True).stdout
        == b"new\n"
    )


@pytest.mark.asyncio
async def test_harbor_write_rejects_directory(tmp_path, harbor_loop):
    path = tmp_path / "directory"
    path.mkdir()
    backend = HarborBackend(_LocalHarborEnvironment(), harbor_loop)

    with pytest.raises(IsADirectoryError):
        await backend.write_bytes(str(path), b"new")

    assert list(path.iterdir()) == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["local", "harbor"])
async def test_overwrite_preserves_plain_file_mode(
    tmp_path, harbor_loop, backend_kind
):
    path = tmp_path / "plain.txt"
    path.write_bytes(b"old")
    path.chmod(0o640)
    backend = (
        LocalFsBackend()
        if backend_kind == "local"
        else HarborBackend(_LocalHarborEnvironment(), harbor_loop)
    )

    await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"new"
    assert _mode(path) == 0o640


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_local_new_file_respects_umask(tmp_path):
    path = tmp_path / "new.txt"
    old_mask = os.umask(0o027)
    try:
        await LocalFsBackend().write_bytes(str(path), b"new")
    finally:
        os.umask(old_mask)

    assert _mode(path) == 0o640
    assert path.read_bytes() == b"new"


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_local_failed_replace_leaves_original_and_cleans_temp(
    tmp_path, monkeypatch
):
    path = tmp_path / "script.sh"
    path.write_bytes(b"old")
    path.chmod(0o755)

    def fail_replace(_source, _destination):
        raise OSError("replace failed")

    monkeypatch.setattr(os, "replace", fail_replace)
    with pytest.raises(OSError, match="replace failed"):
        await LocalFsBackend().write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert _mode(path) == 0o755
    assert not (tmp_path / "script.sh.tmp").exists()


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_harbor_new_file_keeps_emulated_upload_mode(
    tmp_path, harbor_loop
):
    path = tmp_path / "new.txt"
    backend = HarborBackend(_LocalHarborEnvironment(), harbor_loop)

    await backend.write_bytes(str(path), b"new")

    assert _mode(path) == 0o644
    assert os.access(path, os.R_OK | os.W_OK)


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
@pytest.mark.parametrize("backend_kind", ["local", "harbor"])
async def test_overwrite_replaces_symlink_without_changing_target(
    tmp_path, harbor_loop, backend_kind
):
    target = tmp_path / "target.txt"
    target.write_bytes(b"target")
    target.chmod(0o755)
    link = tmp_path / "link.txt"
    link.symlink_to(target)
    backend = (
        LocalFsBackend()
        if backend_kind == "local"
        else HarborBackend(_LocalHarborEnvironment(), harbor_loop)
    )

    await backend.write_bytes(str(link), b"replacement")

    assert not link.is_symlink()
    assert link.read_bytes() == b"replacement"
    assert _mode(link) & 0o111 == 0
    assert target.read_bytes() == b"target"
    assert _mode(target) == 0o755


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_harbor_failed_write_leaves_existing_file_unchanged(
    tmp_path, harbor_loop
):
    path = tmp_path / "script.sh"
    path.write_bytes(b"old")
    path.chmod(0o755)
    environment = _LocalHarborEnvironment()
    environment.fail_write = True
    backend = HarborBackend(environment, harbor_loop)

    with pytest.raises(OSError, match="write failed"):
        await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert _mode(path) == 0o755
    assert not list(tmp_path.glob(".gc-write-*"))


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_harbor_failed_replace_leaves_existing_file_unchanged(
    tmp_path, harbor_loop
):
    path = tmp_path / "script.sh"
    path.write_bytes(b"old")
    path.chmod(0o755)
    environment = _LocalHarborEnvironment()
    environment.fail_replace = True
    backend = HarborBackend(environment, harbor_loop)

    with pytest.raises(OSError, match="replace failed"):
        await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert _mode(path) == 0o755
    assert not list(tmp_path.glob(".gc-write-*"))


@pytest.mark.asyncio
async def test_harbor_prepare_permission_error_does_not_upload(
    tmp_path, harbor_loop
):
    path = tmp_path / "existing.txt"
    path.write_bytes(b"old")
    environment = _LocalHarborEnvironment()
    environment.fail_prepare = True
    backend = HarborBackend(environment, harbor_loop)

    with pytest.raises(PermissionError, match="Permission denied"):
        await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert environment.uploaded == []


@pytest.mark.skipif(os.name == "nt", reason="POSIX file modes are required")
@pytest.mark.asyncio
async def test_harbor_rejects_unwritable_existing_file(tmp_path, harbor_loop):
    path = tmp_path / "read_only.txt"
    path.write_bytes(b"old")
    path.chmod(0o444)
    environment = _LocalHarborEnvironment()
    backend = HarborBackend(environment, harbor_loop)

    with pytest.raises(PermissionError, match="Permission denied"):
        await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert environment.uploaded == []


@pytest.mark.asyncio
async def test_harbor_rejects_malformed_stat_response(tmp_path, harbor_loop):
    path = tmp_path / "existing.txt"
    path.write_bytes(b"old")
    environment = _LocalHarborEnvironment()
    environment.malformed_stat = True
    backend = HarborBackend(environment, harbor_loop)

    with pytest.raises(OSError, match="invalid stat response"):
        await backend.write_bytes(str(path), b"new")

    assert path.read_bytes() == b"old"
    assert not list(tmp_path.glob(".gc-write-*"))
