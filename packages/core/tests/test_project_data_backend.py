from typing import Optional

from gc_core.execution_workspace import ExecutionWorkspace, build_execution_workspace
from gc_core.utils.project_data import get_project_data


class FakeSandboxBackend:
    """Minimal FsBackend stand-in: only what get_project_data uses."""

    is_sandbox = True

    def __init__(self, cwd: str, tree: dict[str, list[tuple[str, bool]]], git: bool):
        self._cwd = cwd
        self._tree = tree  # path -> [(name, is_dir), ...]
        self._git = git

    def default_cwd(self) -> Optional[str]:
        return self._cwd

    async def exists(self, path: str) -> bool:
        if path.endswith("/.git"):
            return self._git
        return path in self._tree

    async def is_dir(self, path: str) -> bool:
        return path in self._tree

    async def listdir(self, path: str) -> list[tuple[str, bool]]:
        return sorted(self._tree.get(path, []))

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    def normpath(self, path: str) -> str:
        return path.rstrip("/") or "/"


def _backend() -> FakeSandboxBackend:
    return FakeSandboxBackend(
        cwd="/testbed",
        tree={
            "/testbed": [("django", True), ("setup.py", False)],
            "/testbed/django": [("apps", True), ("__init__.py", False)],
        },
        git=True,
    )


def _ws(backend: FakeSandboxBackend | None = None) -> ExecutionWorkspace:
    be = backend or _backend()
    return ExecutionWorkspace(root=be.default_cwd() or "/", backend=be, shell="sh", os_name="Linux", root_writable=True)


async def test_sandbox_workspace_supplies_project_dir_and_git_flag():
    data = await get_project_data(workspace=_ws())
    assert data["projectDir"] == "/testbed"
    assert data["isGitProject"] == "true"


async def test_sandbox_workspace_supplies_listing():
    data = await get_project_data(workspace=_ws())
    listing = data["projectDirListing"]
    assert "django/" in listing
    assert "setup.py" in listing
    assert "    __init__.py" in listing  # nested entry, one level deep


async def test_sandbox_workspace_reports_container_writable_and_shell():
    data = await get_project_data(workspace=_ws())
    assert data["projectDirWritable"] == "true"
    assert data["allowed_write_paths"] == "/testbed"
    assert data["shell"] == "sh"


async def test_no_git_dir_reports_false():
    backend = FakeSandboxBackend(cwd="/app", tree={"/app": []}, git=False)
    data = await get_project_data(workspace=_ws(backend))
    assert data["projectDir"] == "/app"
    assert data["isGitProject"] == "false"
    assert data["projectDirListing"] == ""


async def test_local_path_unchanged_when_workspace_is_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    data = await get_project_data(None)
    assert data["projectDir"] == str(tmp_path.resolve())
    assert "a.txt" in data["projectDirListing"]


async def test_non_sandbox_backend_falls_back_to_local(tmp_path, monkeypatch):
    class LocalLike(FakeSandboxBackend):
        is_sandbox = False

    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.txt").write_text("x")
    data = await get_project_data(None, backend=LocalLike(cwd="/testbed", tree={}, git=True))
    assert data["projectDir"] == str(tmp_path.resolve())
    assert "b.txt" in data["projectDirListing"]


class _Exec:
    def __init__(self, stdout: str, return_code: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = return_code
        self.timed_out = False


class ContainerBackend(FakeSandboxBackend):
    """Container backend: default_cwd() is None (the container owns its workdir)."""

    def __init__(self, pwd: str, tree, git: bool, pwd_rc: int = 0):
        super().__init__(cwd="", tree=tree, git=git)
        self._pwd = pwd
        self._pwd_rc = pwd_rc

    def default_cwd(self):
        return None

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        assert command == "pwd"
        return _Exec(self._pwd + "\n", self._pwd_rc)


async def test_container_cwd_is_asked_when_building_workspace():
    backend = ContainerBackend(
        pwd="/app/personal-site",
        tree={"/app/personal-site": [("index.html", False)]},
        git=True,
    )
    ws = build_execution_workspace(backend)
    data = await get_project_data(workspace=ws)
    assert ws.root == "/app/personal-site"
    assert data["projectDir"] == "/app/personal-site"
    assert data["isGitProject"] == "true"
    assert "index.html" in data["projectDirListing"]


async def test_container_cwd_falls_back_to_root_when_pwd_fails():
    backend = ContainerBackend(pwd="", tree={"/": []}, git=False, pwd_rc=1)
    ws = build_execution_workspace(backend)
    assert ws.root == "/"
    assert (await get_project_data(workspace=ws))["projectDir"] == "/"


async def test_container_cwd_falls_back_to_root_when_exec_raises():
    class Broken(ContainerBackend):
        def exec(self, command, *, cwd=None, timeout=None, env=None):
            raise RuntimeError("environment unavailable")

    ws = build_execution_workspace(Broken(pwd="/x", tree={"/": []}, git=False))
    assert ws.root == "/"
    assert (await get_project_data(workspace=ws))["projectDir"] == "/"


async def test_backend_compat_shim_still_builds_sandbox_facts():
    data = await get_project_data(None, backend=_backend())
    assert data["projectDir"] == "/testbed"
