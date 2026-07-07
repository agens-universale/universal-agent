import json
from types import SimpleNamespace

from tests.harbor.fakes import bind_os_backend
from tools.backends.protocol import ExecResult
from tools.exec.execute_command import ExecuteCommand


class _RecordingFakeBackend:
    """Records exec()/is_dir() calls; container semantics (no host cwd default)."""

    default_exec_timeout = 300

    def __init__(self, stdout="", stderr="", rc=0, dirs=None, timed_out=False):
        self.res = ExecResult(stdout=stdout, stderr=stderr, return_code=rc, timed_out=timed_out)
        self.calls = []
        self._dirs = set(dirs or [])

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.calls.append({"command": command, "cwd": cwd, "timeout": timeout, "env": env})
        return self.res

    def is_dir(self, path):
        return path in self._dirs

    def default_cwd(self):
        return None  # container: no implicit host cwd


def _tool(backend):
    t = ExecuteCommand.__new__(ExecuteCommand)
    t._config_dict = None
    return bind_os_backend(t, backend, config_dict={})


def test_envelope_shape_matches_base_contract():
    b = _RecordingFakeBackend(stdout="hi", rc=0)
    env = json.loads(_tool(b)(command="echo hi"))
    # Same fields the base prompt_description advertises:
    for k in ("tool", "ok", "command", "cwd", "exit_code", "timed_out", "truncated", "error", "stdout", "stderr"):
        assert k in env, k
    assert env["exit_code"] == 0 and env["ok"] is True and env["timed_out"] is False


def test_default_timeout_flows_from_backend_default_exec_timeout():
    """When timeout_seconds is omitted, harbor-mode backend default (300) is used —
    NOT the base class default of 30."""
    b = _RecordingFakeBackend()
    _tool(b)(command="x")
    assert b.calls[0]["timeout"] == 300


def test_explicit_timeout_seconds_forwarded():
    b = _RecordingFakeBackend()
    _tool(b)(command="x", timeout_seconds=120)
    assert b.calls[0]["timeout"] == 120


def test_output_truncated_to_max_output_chars():
    b = _RecordingFakeBackend(stdout="A" * 10000)
    env = json.loads(_tool(b)(command="x", max_output_chars=100))
    assert len(env["stdout"]) <= 100 and env["truncated"] is True


def test_nonzero_exit_sets_error():
    b = _RecordingFakeBackend(stderr="boom", rc=2)
    env = json.loads(_tool(b)(command="x"))
    assert env["ok"] is False and env["exit_code"] == 2
    assert env["error"]["type"] == "non_zero_exit"


def test_empty_command_keeps_base_message():
    env = json.loads(_tool(_RecordingFakeBackend())(command="   "))
    assert env["error"]["field"] == "command"
    assert env["error"]["message"] == "Empty command"  # base parity


def test_timed_out_result_sets_timeout_envelope():
    b = _RecordingFakeBackend(timed_out=True)
    env = json.loads(_tool(b)(command="x", timeout_seconds=1))
    assert env["ok"] is False
    assert env["timed_out"] is True
    assert env["exit_code"] is None
    assert env["error"]["type"] == "timeout"


def test_default_cwd_none_is_passed_through_untouched():
    """Backend.default_cwd() == None (container semantics) — no host cwd is injected,
    and cwd validation is skipped (cwd_used stays None)."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x"))
    assert env["ok"] is True
    assert env["cwd"] == ""
    assert b.calls[0]["cwd"] is None


# --- Gap 1: env_vars validation and forwarding as a raw dict via backend.exec(env=...) ---

def test_env_vars_forwarded_as_env_kwarg():
    """env_vars must be forwarded to backend.exec() as the env= dict — the base tool
    does not embed them into the command string."""
    b = _RecordingFakeBackend(stdout="bar")
    _tool(b)(command="printenv FOO", env_vars=[{"name": "FOO", "value": "bar"}])
    assert len(b.calls) == 1
    assert b.calls[0]["command"] == "printenv FOO"
    assert b.calls[0]["env"] == {"FOO": "bar"}


def test_no_env_vars_passes_none_env():
    b = _RecordingFakeBackend()
    _tool(b)(command="x")
    assert b.calls[0]["env"] is None


def test_env_vars_not_a_list_returns_invalid_argument():
    """env_vars that is not a list must return invalid_argument with field='env_vars'."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars="FOO=bar"))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "env_vars"
    assert len(b.calls) == 0  # backend must NOT be called


def test_env_vars_item_missing_name_returns_invalid_argument():
    """env_vars item without 'name' must return invalid_argument."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars=[{"value": "bar"}]))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert "env_vars" in env["error"]["field"]
    assert len(b.calls) == 0


def test_env_vars_item_not_dict_returns_invalid_argument():
    """env_vars item that is not a dict must return invalid_argument."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", env_vars=["FOO=bar"]))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert "env_vars" in env["error"]["field"]
    assert len(b.calls) == 0


# --- Gap 2: timeout_seconds and max_output_chars validation ---

def test_timeout_seconds_zero_returns_invalid_argument():
    """timeout_seconds=0 must return invalid_argument with field='timeout_seconds'."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", timeout_seconds=0))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout_seconds"
    assert len(b.calls) == 0


def test_timeout_seconds_negative_returns_invalid_argument():
    """timeout_seconds<0 must return invalid_argument."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", timeout_seconds=-5))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout_seconds"


def test_max_output_chars_zero_returns_invalid_argument():
    """max_output_chars=0 must return invalid_argument with field='max_output_chars'."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", max_output_chars=0))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "max_output_chars"
    assert len(b.calls) == 0


# --- Gap 3: cwd validation via backend.is_dir ---

def test_cwd_nonexistent_in_container_returns_invalid_argument():
    """cwd that doesn't exist in the container must return invalid_argument with field='cwd'."""
    b = _RecordingFakeBackend(dirs=set())  # no dirs registered
    env = json.loads(_tool(b)(command="x", cwd="/nonexistent/path"))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "cwd"
    assert len(b.calls) == 0


def test_cwd_existing_in_container_is_accepted():
    """cwd that exists in the container must be forwarded without error."""
    b = _RecordingFakeBackend(dirs={"/workspace"})
    env = json.loads(_tool(b)(command="x", cwd="/workspace"))
    assert env["ok"] is True
    assert b.calls[0]["cwd"] == "/workspace"
