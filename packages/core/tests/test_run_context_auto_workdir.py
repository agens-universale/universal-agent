# packages/core/tests/test_run_context_auto_workdir.py
"""sandbox.workdir=auto: созданный каталог становится launch_root для guards и workspace."""

import os

from gc_core.backends.local import LocalFsBackend
from gc_core.run_context import acreate_run_context
from gc_core.settings import runtime_of
from gc_testkit.settings import make_settings


def _cfg(raw: dict):
    """Плоский словарь develop → настройки (корни — ``RuntimeEnv``)."""
    paths = {
        "model": "agent.model",
        "system_prompt_id": "agent.system_prompt_id",
        "save_logs": "logging.save_logs",
    }
    return make_settings({paths.get(k, k): v for k, v in raw.items()})


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
    ctx = await acreate_run_context(
        _cfg(cfg), os_backend=AutoDirBackend(str(auto))
    )
    assert runtime_of(ctx).launch_root == str(auto)
    assert os.path.realpath(ctx.workspace.root) == os.path.realpath(str(auto))


async def test_auto_workdir_overrides_runtime_launch_root(tmp_path):
    # TUI/ACP ставят launch_root=cwd ДО сборки контекста (apply_runtime_fields):
    # каталог рана всё равно обязан стать периметром, иначе guards и бэкенд
    # разойдутся — файловые тулы пишут в реальный проект, shell — в auto.
    auto = tmp_path / "sb"
    auto.mkdir()
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": str(tmp_path),
        "project_root": str(tmp_path),
    }
    ctx = await acreate_run_context(
        _cfg(cfg), os_backend=AutoDirBackend(str(auto))
    )
    assert runtime_of(ctx).launch_root == str(auto)
    assert os.path.realpath(ctx.workspace.root) == os.path.realpath(str(auto))


class RemoteFake:
    """Удалённая ФС: каталоги только под /data; cwd той стороны — /data."""

    def __init__(self, files):
        from gc_testkit.harbor_fakes import FakeBackend

        self._inner = FakeBackend(files)

    def __getattr__(self, name):
        return getattr(self._inner, name)

    @property
    def is_sandbox(self):
        return True

    def default_cwd(self):
        return "/data"


async def test_host_launch_root_is_replaced_by_remote_cwd(tmp_path):
    # TUI/ACP ставят launch_root=cwd хоста до сборки; у remote-бэкенда такого
    # каталога нет — корнем обязан стать cwd той стороны, иначе env-блок и
    # guards смотрят на путь хоста.
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": str(tmp_path),
        "project_root": str(tmp_path),
    }
    ctx = await acreate_run_context(
        _cfg(cfg),
        os_backend=RemoteFake({"/data/a.txt": "x", "/data/sub/b.txt": "y"}),
    )
    assert runtime_of(ctx).launch_root == "/data"
    assert ctx.workspace.root == "/data"


async def test_explicit_remote_launch_root_is_kept():
    cfg = {
        "model": "m",
        "system_prompt_id": "v1",
        "save_logs": False,
        "launch_root": "/data/sub",
    }
    ctx = await acreate_run_context(
        _cfg(cfg),
        os_backend=RemoteFake({"/data/a.txt": "x", "/data/sub/b.txt": "y"}),
    )
    assert runtime_of(ctx).launch_root == "/data/sub"
    assert ctx.workspace.root == "/data/sub"
