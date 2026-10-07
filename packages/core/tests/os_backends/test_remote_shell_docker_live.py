# packages/core/tests/os_backends/test_remote_shell_docker_live.py
"""Живой docker: GC_TEST_DOCKER_IMAGE=python:3.12-slim uv run pytest … -q"""

import os
import shutil
import subprocess
import uuid

import pytest
from gc_core.backends.remote_shell import RemoteShellBackend

IMAGE = os.environ.get("GC_TEST_DOCKER_IMAGE")
pytestmark = pytest.mark.skipif(
    not IMAGE or shutil.which("docker") is None,
    reason="set GC_TEST_DOCKER_IMAGE and have docker",
)


@pytest.fixture(scope="module")
def container():
    name = f"gc-rs-{uuid.uuid4().hex[:8]}"
    subprocess.run(
        ["docker", "run", "-d", "--rm", "--name", name, IMAGE, "sleep", "600"],
        check=True,
    )
    yield name
    subprocess.run(["docker", "rm", "-f", name], check=False)


async def test_full_fs_group_inside_container(container):
    be = RemoteShellBackend(["docker", "exec", "-i", container], cwd="/tmp")
    await be.makedirs("/tmp/w/d")
    await be.write("/tmp/w/d/x.txt", "12345")
    await be.write_bytes("/tmp/w/b.bin", bytes(range(256)))
    assert await be.getsize("/tmp/w/d/x.txt") == 5
    assert await be.read_bytes("/tmp/w/b.bin") == bytes(range(256))
    assert await be.listdir("/tmp/w") == [("b.bin", False), ("d", True)]
    rels = {e.rel for e in await be.walk_entries("/tmp/w")}
    assert rels == {"b.bin", "d", "d/x.txt"}
    assert (await be.aexec("cat d/x.txt", cwd="/tmp/w")).stdout == "12345"
    await be.awarm_capabilities()
    assert be.has_find and be.has_grep
    assert await be.aprime_default_cwd() == "/tmp"
