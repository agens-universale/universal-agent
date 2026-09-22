"""Base RunPythonCode routes physical execution through FsBackend.run_python.

The tool's job is validation + envelope shaping; heredoc wrapping and env-prefix
strings are backend concerns (HarborBackend, Task 11) and are no longer built by
the tool. This test drives the base tool with a recording fake sandbox backend
and asserts it forwards the RAW code/cwd/timeout/validated-env dict to
`run_python()`, plus the sandbox cwd branch and shared envelope parity
(validation errors, non_zero_exit, timeout, execution_failed, truncation).
"""
import json
from types import SimpleNamespace

from gc_testkit.harbor_fakes import bind_os_backend
from gc_core.backends.protocol import ExecResult, bound_exec_result
from gc_core.tools.exec.run_python_code import RunPythonCode


class _RecordingFakeBackend:
    """Records run_python()/is_dir() calls; sandbox semantics (is_sandbox=True)."""

    is_sandbox = True

    def __init__(self, stdout="", stderr="", rc=0, dirs=None, timed_out=False, capture_max_chars=None):
        res = ExecResult(stdout=stdout, stderr=stderr, return_code=rc, timed_out=timed_out)
        self.res = res if capture_max_chars is None else bound_exec_result(res, capture_max_chars)
        self.calls = []
        self._dirs = set(dirs or [])

    def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
        self.calls.append({"code": code, "cwd": cwd, "timeout": timeout, "env": env})
        return self.res

    async def is_dir(self, path):
        return path in self._dirs


def _tool(backend, config_dict=None):
    t = RunPythonCode.__new__(RunPythonCode)
    cfg = config_dict if config_dict is not None else {}
    t._config_dict = cfg
    return bind_os_backend(t, backend, config_dict=cfg)


def test_envelope_shape_matches_base_contract():
    b = _RecordingFakeBackend(stdout="42", rc=0)
    env = json.loads(_tool(b)(code="print(42)"))
    for k in ("tool", "ok", "return_code", "timed_out", "error", "stdout", "stderr"):
        assert k in env, k
    assert "truncated" not in env
    assert env["return_code"] == 0 and env["ok"] is True


def test_default_timeout_is_schema_default():
    b = _RecordingFakeBackend()
    _tool(b)(code="x=1")
    assert b.calls[0]["timeout"] == 120


def test_default_timeout_flows_from_exec_timeout_sec_config():
    b = _RecordingFakeBackend()
    _tool(b, config_dict={"exec_timeout_sec": 600})(code="x=1")
    assert b.calls[0]["timeout"] == 600


def test_explicit_timeout_forwarded():
    b = _RecordingFakeBackend()
    _tool(b)(code="x=1", timeout=42)
    assert b.calls[0]["timeout"] == 42


def test_code_forwarded_raw_no_heredoc_wrapping():
    """The tool no longer builds a `python3 - <<'DELIM' ... DELIM` heredoc — that
    shaping moved to HarborBackend. The backend receives the code verbatim."""
    b = _RecordingFakeBackend()
    _tool(b)(code="print('hi')")
    assert b.calls[0]["code"] == "print('hi')"


def test_truncation():
    env = json.loads(
        _tool(
            _RecordingFakeBackend(stdout="A" * 9000, capture_max_chars=50),
            config_dict={
                "exec_output_capture_max_chars": 50,
                "exec_output_preview_edge_chars": 5,
            },
        )(code="x")
    )
    assert env["truncated"] is True
    assert len(env["stdout"]["head"]) == 25
    assert len(env["stdout"]["tail"]) == 25
    assert env["stdout"]["omitted_chars"] == 8950
    assert env["preview"]["stdout"]["head"] == "A" * 5


def test_invalid_code_rejected():
    env = json.loads(_tool(_RecordingFakeBackend())(code="   "))
    assert env["ok"] is False and env["error"]["field"] == "code"


def test_invalid_timeout_rejected():
    env = json.loads(_tool(_RecordingFakeBackend())(code="x", timeout=0))
    assert env["ok"] is False and env["error"]["field"] == "timeout"


def test_max_output_chars_argument_no_longer_exists():
    env = json.loads(_tool(_RecordingFakeBackend(stdout="hi"))(code="x", max_output_chars=0))
    assert env["ok"] is True and env["stdout"]["content"] == "hi"


def test_sandbox_cwd_none_passes_through_untouched():
    """Sandbox branch: cwd omitted -> cwd_used stays None (container default
    workdir); is_dir() is not consulted, no host security_context is built."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(code="x"))
    assert env["ok"] is True
    assert b.calls[0]["cwd"] is None


def test_sandbox_cwd_not_a_directory_rejected():
    env = json.loads(_tool(_RecordingFakeBackend())(code="x", cwd="/nope"))
    assert env["ok"] is False and env["error"]["type"] == "cwd_invalid"
    # ponytail: sandbox branch has no host security_context, so allowed_write_paths
    # is always empty (moved verbatim from the old harbor shim).
    assert env["error"]["allowed_write_paths"] == []
    assert "hint" in env["error"]


def test_sandbox_valid_cwd_forwarded_raw():
    b = _RecordingFakeBackend(dirs={"/work"})
    env = json.loads(_tool(b)(code="x", cwd="/work"))
    assert env["ok"] is True and b.calls[0]["cwd"] == "/work"


def test_env_vars_forwarded_as_dict_not_prefix_string():
    """env_vars is validated into a {name: value} dict and forwarded via the
    backend's env= kwarg — no `env FOO=bar` shell-prefix string is built here."""
    b = _RecordingFakeBackend()
    _tool(b)(code="x", env_vars=[{"name": "FOO", "value": "bar"}])
    assert b.calls[0]["env"] == {"FOO": "bar"}


def test_no_env_vars_passes_none_env():
    b = _RecordingFakeBackend()
    _tool(b)(code="x")
    assert b.calls[0]["env"] is None


def test_env_vars_malformed_rejected():
    env = json.loads(_tool(_RecordingFakeBackend())(code="x", env_vars=[{"value": "bar"}]))
    assert env["ok"] is False and env["error"]["field"] == "env_vars[0].name"


def test_non_zero_exit_sets_error():
    b = _RecordingFakeBackend(rc=3)
    env = json.loads(_tool(b)(code="import sys; sys.exit(3)"))
    assert env["ok"] is False and env["return_code"] == 3
    assert env["error"]["type"] == "non_zero_exit"


def test_backend_timed_out_sets_timeout_envelope():
    b = _RecordingFakeBackend(timed_out=True)
    env = json.loads(_tool(b)(code="x", timeout=1))
    assert env["ok"] is False
    assert env["timed_out"] is True
    assert env["return_code"] is None
    assert env["error"]["type"] == "timeout"
    assert env["error"]["message"] == "Execution timed out after 1 seconds"


def test_backend_exception_returns_execution_failed():
    class _RaisingBackend(_RecordingFakeBackend):
        def run_python(self, code, *, cwd=None, timeout=None, env=None, capture_max_chars=None):
            raise RuntimeError("boom")

    env = json.loads(_tool(_RaisingBackend())(code="x"))
    assert env["ok"] is False
    assert env["error"]["type"] == "execution_failed"
    assert env["error"]["message"] == "boom"
