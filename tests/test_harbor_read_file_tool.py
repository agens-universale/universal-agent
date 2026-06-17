from types import SimpleNamespace
from tools.harbor.read_file import HarborReadFile
from tools.harbor.execute_command import HarborExecuteCommand


class FakeBackend:
    def __init__(self):
        self.reads = []
        self.execs = []

    def read(self, path):
        self.reads.append(path)
        return "FILE CONTENT"

    def exec(self, command, **kw):
        self.execs.append(command)
        from tools.harbor.backend import ExecResult
        return ExecResult(stdout="OUT", stderr="", return_code=0)


def _ctx(backend):
    return SimpleNamespace(harbor_backend=backend, config_dict={})


def test_harbor_read_file_uses_backend():
    backend = FakeBackend()
    tool = HarborReadFile(ctx=_ctx(backend), config_dict={})
    out = tool(path="/work/a.txt")
    assert "FILE CONTENT" in out
    assert backend.reads == ["/work/a.txt"]


def test_harbor_read_file_keeps_registered_name():
    assert HarborReadFile.registered_tool_name() == "fs_read_file"


def test_harbor_execute_command_uses_backend():
    backend = FakeBackend()
    tool = HarborExecuteCommand(ctx=_ctx(backend), config_dict={})
    out = tool(command="echo hi")
    assert "OUT" in out
    assert backend.execs == ["echo hi"]


def test_harbor_execute_command_keeps_registered_name():
    assert HarborExecuteCommand.registered_tool_name() == "exec_command"
