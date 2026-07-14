from typing import Optional

from utils.project_data import get_project_data


class FakeSandboxBackend:
    """Minimal FsBackend stand-in: only what get_project_data uses."""

    is_sandbox = True

    def __init__(self, cwd: str, tree: dict[str, list[tuple[str, bool]]], git: bool):
        self._cwd = cwd
        self._tree = tree          # path -> [(name, is_dir), ...]
        self._git = git

    def default_cwd(self) -> Optional[str]:
        return self._cwd

    def exists(self, path: str) -> bool:
        if path.endswith("/.git"):
            return self._git
        return path in self._tree

    def is_dir(self, path: str) -> bool:
        return path in self._tree

    def listdir(self, path: str) -> list[tuple[str, bool]]:
        return sorted(self._tree.get(path, []))

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name


def _backend() -> FakeSandboxBackend:
    return FakeSandboxBackend(
        cwd="/testbed",
        tree={
            "/testbed": [("django", True), ("setup.py", False)],
            "/testbed/django": [("apps", True), ("__init__.py", False)],
        },
        git=True,
    )


def test_sandbox_backend_supplies_project_dir_and_git_flag():
    data = get_project_data(None, backend=_backend())
    assert data["projectDir"] == "/testbed"
    assert data["isGitProject"] == "true"


def test_sandbox_backend_supplies_listing():
    data = get_project_data(None, backend=_backend())
    listing = data["projectDirListing"]
    assert "django/" in listing
    assert "setup.py" in listing
    assert "    __init__.py" in listing          # nested entry, one level deep


def test_sandbox_backend_reports_container_writable_and_shell():
    data = get_project_data(None, backend=_backend())
    assert data["projectDirWritable"] == "true"
    assert data["allowed_write_paths"] == "/testbed"
    assert data["shell"] == "sh"


def test_no_git_dir_reports_false():
    backend = FakeSandboxBackend(cwd="/app", tree={"/app": []}, git=False)
    data = get_project_data(None, backend=backend)
    assert data["projectDir"] == "/app"
    assert data["isGitProject"] == "false"
    assert data["projectDirListing"] == ""


def test_local_path_unchanged_when_backend_is_none(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "a.txt").write_text("x")
    data = get_project_data(None, backend=None)
    assert data["projectDir"] == str(tmp_path.resolve())
    assert "a.txt" in data["projectDirListing"]


def test_non_sandbox_backend_falls_back_to_local(tmp_path, monkeypatch):
    class LocalLike(FakeSandboxBackend):
        is_sandbox = False

    monkeypatch.chdir(tmp_path)
    (tmp_path / "b.txt").write_text("x")
    data = get_project_data(None, backend=LocalLike(cwd="/testbed", tree={}, git=True))
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


def test_container_cwd_is_asked_from_the_environment():
    backend = ContainerBackend(
        pwd="/app/personal-site",
        tree={"/app/personal-site": [("index.html", False)]},
        git=True,
    )
    data = get_project_data(None, backend=backend)
    assert data["projectDir"] == "/app/personal-site"
    assert data["isGitProject"] == "true"
    assert "index.html" in data["projectDirListing"]


def test_container_cwd_falls_back_to_root_when_pwd_fails():
    backend = ContainerBackend(pwd="", tree={"/": []}, git=False, pwd_rc=1)
    data = get_project_data(None, backend=backend)
    assert data["projectDir"] == "/"


def test_container_cwd_falls_back_to_root_when_exec_raises():
    class Broken(ContainerBackend):
        def exec(self, command, *, cwd=None, timeout=None, env=None):
            raise RuntimeError("environment unavailable")

    data = get_project_data(None, backend=Broken(pwd="/x", tree={"/": []}, git=False))
    assert data["projectDir"] == "/"
