# packages/core/tests/test_run_context_auto_workdir.py
"""sandbox.workdir=auto: созданный каталог становится launch_root для guards и workspace."""

import os

from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import acreate_run_context


class AutoDirBackend(LocalFsBackend):
    def __init__(self, auto):
        super().__init__()
        self.auto_workdir = auto

    def default_cwd(self):
        return self.auto_workdir


async def test_auto_workdir_becomes_launch_root(tmp_path, monkeypatch):
    monkeypatch.setenv("GC_STATE_DIR", str(tmp_path / "state"))
    auto = tmp_path / "sb"
    auto.mkdir()
    cfg = {"model": "m", "system_prompt_id": "v1", "save_logs": False}
    ctx = await acreate_run_context(cfg, os_backend=AutoDirBackend(str(auto)))
    assert cfg["launch_root"] == str(auto)
    assert os.path.realpath(ctx.workspace.root) == os.path.realpath(str(auto))


async def test_explicit_launch_root_wins(tmp_path):
    auto = tmp_path / "sb"
    auto.mkdir()
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": str(tmp_path),
    }
    await acreate_run_context(cfg, os_backend=AutoDirBackend(str(auto)))
    assert cfg["launch_root"] == str(tmp_path)
