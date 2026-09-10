from gc_core.execution_workspace import ExecutionWorkspace, build_execution_workspace, discover_backend_cwd, resolve_execution_root
from gc_core.backends.local import LocalFsBackend
from gc_core.utils.project_data import get_project_data
from gc_core.utils.tool_report_paths import tool_report_abspath


class _Exec:
    def __init__(self, stdout: str, return_code: int = 0):
        self.stdout = stdout
        self.stderr = ""
        self.return_code = return_code
        self.timed_out = False


class SandboxBackend:
    is_sandbox = True

    def __init__(self, *, cwd=None, pwd=None, pwd_rc=0):
        self._cwd = cwd
        self._pwd = pwd
        self._pwd_rc = pwd_rc

    def default_cwd(self):
        return self._cwd

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        assert command == "pwd"
        return _Exec((self._pwd or "") + "\n", self._pwd_rc)

    def normpath(self, path: str) -> str:
        return path.rstrip("/") or "/"

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    async def exists(self, path: str) -> bool:
        return False

    async def listdir(self, path: str):
        return []


def test_local_workspace_uses_launch_root(tmp_path):
    be = LocalFsBackend()
    ws = build_execution_workspace(be, {"launch_root": str(tmp_path)})
    assert ws.root == str(tmp_path.resolve())
    assert ws.backend is be


def test_sandbox_workspace_uses_declared_cwd():
    be = SandboxBackend(cwd="/testbed")
    ws = build_execution_workspace(be)
    assert ws.root == "/testbed"
    assert ws.shell == "sh"
    assert ws.os_name == "Linux"
    assert ws.root_writable is True


def test_sandbox_workspace_asks_pwd_when_cwd_undeclared():
    be = SandboxBackend(cwd=None, pwd="/app/personal-site")
    assert discover_backend_cwd(be) == "/app/personal-site"
    ws = build_execution_workspace(be)
    assert ws.root == "/app/personal-site"
    assert ws.shell == "sh"
    assert ws.os_name == "Linux"
    assert ws.root_writable is True


def test_sandbox_config_root_overrides_pwd():
    be = SandboxBackend(cwd=None, pwd="/app/other")
    root = resolve_execution_root(be, {"project_root": "/testbed"})
    assert root == "/testbed"


async def test_project_data_and_tool_paths_share_workspace_root():
    be = SandboxBackend(cwd=None, pwd="/testbed")
    ws = ExecutionWorkspace(root="/testbed", backend=be, shell="sh", os_name="Linux", root_writable=True)
    data = await get_project_data(workspace=ws)
    assert data["projectDir"] == "/testbed"
    assert data["shell"] == "sh"
    assert data["OS"] == "Linux"
    assert tool_report_abspath("pkg/a.py", workspace=ws) == "/testbed/pkg/a.py"


def test_tool_report_does_not_fall_back_to_slash_when_workspace_has_root():
    """Regression: default_cwd() is None, but workspace.root must still win."""
    be = SandboxBackend(cwd=None, pwd="/testbed")
    ws = ExecutionWorkspace(root="/testbed", backend=be, shell="sh", os_name="Linux", root_writable=True)
    assert tool_report_abspath("mod.py", workspace=ws) == "/testbed/mod.py"


def test_local_workspace_shell_from_env(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("SHELL", "/bin/zsh")
    ws = build_execution_workspace(LocalFsBackend())
    assert ws.shell == "/bin/zsh"
