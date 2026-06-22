import json
from types import SimpleNamespace

from tools.harbor.backend import ExecResult
from tools.harbor.execute_command import HarborExecuteCommand


class _FakeBackend:
    def __init__(self, stdout="", stderr="", rc=0, dirs=None):
        self.res = ExecResult(stdout=stdout, stderr=stderr, return_code=rc)
        self.calls = []
        self._dirs = set(dirs or [])

    def exec(self, command, *, cwd=None, timeout=None):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout})
        return self.res

    def is_dir(self, path):
        return path in self._dirs


def _tool(backend):
    t = HarborExecuteCommand.__new__(HarborExecuteCommand)
    t._ctx = SimpleNamespace(harbor_backend=backend)
    return t


def test_envelope_shape_matches_base_contract():
    b = _FakeBackend(stdout="hi", rc=0)
    env = json.loads(_tool(b)(command="echo hi"))
    # Same fields the base prompt_description advertises:
    for k in ("tool", "ok", "command", "cwd", "exit_code", "timed_out", "truncated", "error", "stdout", "stderr"):
        assert k in env, k
    assert env["exit_code"] == 0 and env["ok"] is True and env["timed_out"] is False


def test_default_timeout_applied():
    b = _FakeBackend()
    _tool(b)(command="x")
    assert b.calls[0]["timeout"] == 30  # base default


def test_explicit_timeout_seconds_forwarded():
    b = _FakeBackend()
    _tool(b)(command="x", timeout_seconds=120)
    assert b.calls[0]["timeout"] == 120


def test_output_truncated_to_max_output_chars():
    b = _FakeBackend(stdout="A" * 10000)
    env = json.loads(_tool(b)(command="x", max_output_chars=100))
    assert len(env["stdout"]) <= 100 and env["truncated"] is True


def test_nonzero_exit_sets_error():
    b = _FakeBackend(stderr="boom", rc=2)
    env = json.loads(_tool(b)(command="x"))
    assert env["ok"] is False and env["exit_code"] == 2
    assert env["error"]["type"] == "non_zero_exit"


def test_empty_command_keeps_base_message():
    env = json.loads(_tool(_FakeBackend())(command="   "))
    assert env["error"]["field"] == "command"
    assert env["error"]["message"] == "Empty command"  # base parity


# --- Gap 1: env_vars validation and forwarding ---

def test_env_vars_forwarded_as_inline_prefix():
    """env_vars must be forwarded to the container command as inline env prefix."""
    b = _FakeBackend(stdout="bar")
    _tool(b)(command="printenv FOO", env_vars=[{"name": "FOO", "value": "bar"}])
    assert len(b.calls) == 1
    cmd = b.calls[0]["command"]
    assert "FOO=" in cmd or "FOO=bar" in cmd, f"env prefix not found in command: {cmd!r}"
    assert "bar" in cmd, f"value 'bar' not found in command: {cmd!r}"


def test_env_vars_not_a_list_returns_invalid_argument():
    """env_vars that is not a list must return invalid_argument with field='env_vars'."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars="FOO=bar"))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "env_vars"
    assert len(b.calls) == 0  # backend must NOT be called


def test_env_vars_item_missing_name_returns_invalid_argument():
    """env_vars item without 'name' must return invalid_argument."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars=[{"value": "bar"}]))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert "env_vars" in env["error"]["field"]
    assert len(b.calls) == 0


def test_env_vars_item_not_dict_returns_invalid_argument():
    """env_vars item that is not a dict must return invalid_argument."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars=["FOO=bar"]))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert "env_vars" in env["error"]["field"]
    assert len(b.calls) == 0


# --- Gap 2: timeout_seconds and max_output_chars validation ---

def test_timeout_seconds_zero_returns_invalid_argument():
    """timeout_seconds=0 must return invalid_argument with field='timeout_seconds'."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", timeout_seconds=0))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout_seconds"
    assert len(b.calls) == 0


def test_timeout_seconds_negative_returns_invalid_argument():
    """timeout_seconds<0 must return invalid_argument."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", timeout_seconds=-5))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout_seconds"


def test_max_output_chars_zero_returns_invalid_argument():
    """max_output_chars=0 must return invalid_argument with field='max_output_chars'."""
    b = _FakeBackend()
    env = json.loads(_tool(b)(command="x", max_output_chars=0))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "max_output_chars"
    assert len(b.calls) == 0


# --- Gap 3: cwd validation via backend.is_dir ---

def test_cwd_nonexistent_in_container_returns_invalid_argument():
    """cwd that doesn't exist in the container must return invalid_argument with field='cwd'."""
    b = _FakeBackend(dirs=set())  # no dirs registered
    env = json.loads(_tool(b)(command="x", cwd="/nonexistent/path"))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "cwd"
    assert len(b.calls) == 0


def test_cwd_existing_in_container_is_accepted():
    """cwd that exists in the container must be forwarded without error."""
    b = _FakeBackend(dirs={"/workspace"})
    env = json.loads(_tool(b)(command="x", cwd="/workspace"))
    assert env["ok"] is True
    assert b.calls[0]["cwd"] == "/workspace"
