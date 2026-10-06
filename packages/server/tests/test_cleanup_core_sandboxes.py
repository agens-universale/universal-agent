"""Janitor чистит и песочницы ядра (os_backend=sandbox, workdir=auto)."""

import os
import time

from gc_server.swarm.cleanup import cleanup_threads


def test_extra_sandbox_roots_are_swept(tmp_path):
    threads = tmp_path / "threads"
    threads.mkdir()
    swarm_sb = tmp_path / "swarm_sb"
    swarm_sb.mkdir()
    core_sb = tmp_path / "core_sb"
    (core_sb / "old").mkdir(parents=True)
    (core_sb / "new").mkdir()
    old = time.time() - 3 * 3600
    os.utime(core_sb / "old", (old, old))
    stats = cleanup_threads(
        str(threads), str(swarm_sb), 1.0, extra_sandbox_roots=[str(core_sb)]
    )
    assert stats["sandboxes_removed"] == 1
    assert not (core_sb / "old").exists() and (core_sb / "new").exists()


def test_extra_roots_swept_even_without_threads_dir(tmp_path):
    core_sb = tmp_path / "core_sb"
    (core_sb / "old").mkdir(parents=True)
    old = time.time() - 3 * 3600
    os.utime(core_sb / "old", (old, old))
    stats = cleanup_threads(
        str(tmp_path / "no-threads"),
        str(tmp_path / "no-swarm"),
        1.0,
        extra_sandbox_roots=[str(core_sb)],
    )
    assert stats["sandboxes_removed"] == 1
