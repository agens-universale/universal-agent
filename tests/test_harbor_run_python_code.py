"""HarborRunPythonCode must match the base RunPythonCode output contract.

Runs in the container, so it can't be diffed against the host-subprocess base; we verify
the advertised envelope shape (tool/ok/return_code/timed_out/truncated/error/stdout/stderr),
the default timeout (10s), and output truncation against a fake backend.
"""
import json
from types import SimpleNamespace

from tools.harbor.backend import ExecResult
from tools.harbor.run_python_code import HarborRunPythonCode


class _FakeBackend:
    def __init__(self, stdout="", stderr="", rc=0, dirs=("/work",)):
        self.res = ExecResult(stdout=stdout, stderr=stderr, return_code=rc)
        self.calls = []
        self._dirs = set(dirs)

    def exec(self, command, *, cwd=None, timeout=None):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        return self.res

    def is_dir(self, p):
        return p in self._dirs


def _tool(b):
    t = HarborRunPythonCode.__new__(HarborRunPythonCode)
    t._ctx = SimpleNamespace(harbor_backend=b)
    return t


def test_envelope_shape():
    env = json.loads(_tool(_FakeBackend(stdout="42"))(code="print(42)"))
    for k in ("tool", "ok", "return_code", "timed_out", "truncated", "error", "stdout", "stderr"):
        assert k in env, k
    assert env["return_code"] == 0 and env["ok"] is True


def test_default_timeout_is_base_default():
    b = _FakeBackend()
    _tool(b)(code="x=1")
    assert b.calls[0]["timeout"] == 10


def test_code_is_wrapped_for_python():
    b = _FakeBackend()
    _tool(b)(code="print('hi')")
    assert "python3" in b.calls[0]["command"]
    assert "print('hi')" in b.calls[0]["command"]


def test_truncation():
    env = json.loads(_tool(_FakeBackend(stdout="A" * 9000))(code="x", max_output_chars=50))
    assert len(env["stdout"]) <= 50 and env["truncated"] is True


def test_invalid_code_rejected():
    env = json.loads(_tool(_FakeBackend())(code="   "))
    assert env["ok"] is False and env["error"]["field"] == "code"


def test_invalid_timeout_rejected():
    env = json.loads(_tool(_FakeBackend())(code="x", timeout_seconds=0))
    assert env["ok"] is False and env["error"]["field"] == "timeout_seconds"


def test_invalid_max_output_rejected():
    env = json.loads(_tool(_FakeBackend())(code="x", max_output_chars=0))
    assert env["ok"] is False and env["error"]["field"] == "max_output_chars"


def test_cwd_not_a_directory_rejected():
    env = json.loads(_tool(_FakeBackend())(code="x", cwd="/nope"))
    assert env["ok"] is False and env["error"]["type"] == "cwd_invalid"
    assert "hint" in env["error"]  # base parity: cwd_invalid carries a hint


def test_valid_cwd_passes_through():
    b = _FakeBackend()
    env = json.loads(_tool(b)(code="x", cwd="/work"))
    assert env["ok"] is True and b.calls[0]["cwd"] == "/work"


def test_env_vars_forwarded_as_prefix():
    b = _FakeBackend()
    _tool(b)(code="x", env_vars=[{"name": "FOO", "value": "bar"}])
    assert "env FOO=bar" in b.calls[0]["command"]


def test_env_vars_malformed_rejected():
    env = json.loads(_tool(_FakeBackend())(code="x", env_vars=[{"value": "bar"}]))
    assert env["ok"] is False and env["error"]["field"] == "env_vars[0].name"
