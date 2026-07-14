import os

from execution_workspace import ExecutionWorkspace
from utils.tool_report_paths import tool_report_abspath


class FakeSandbox:
    is_sandbox = True

    def default_cwd(self):
        return "/testbed"

    def join(self, base: str, name: str) -> str:
        return base.rstrip("/") + "/" + name

    def normpath(self, path: str) -> str:
        return os.path.normpath(path)


def _ws() -> ExecutionWorkspace:
    return ExecutionWorkspace(root="/testbed", backend=FakeSandbox(), shell="sh", os_name="Linux", root_writable=True)


def test_relative_path_resolves_against_workspace_root():
    assert tool_report_abspath(".", workspace=_ws()) == "/testbed"
    assert tool_report_abspath("pkg/mod.py", workspace=_ws()) == "/testbed/pkg/mod.py"


def test_absolute_path_is_kept_and_normalized():
    assert tool_report_abspath("/testbed/./pkg/../mod.py", workspace=_ws()) == "/testbed/mod.py"


def test_without_workspace_uses_host_abspath(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    assert tool_report_abspath("x.txt") == str(tmp_path / "x.txt")


def test_none_and_empty_pass_through():
    assert tool_report_abspath(None, workspace=_ws()) is None
    assert tool_report_abspath("", workspace=_ws()) == ""


def test_backend_compat_shim_still_works():
    assert tool_report_abspath("pkg/mod.py", backend=FakeSandbox()) == "/testbed/pkg/mod.py"
