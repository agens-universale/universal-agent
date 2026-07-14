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
