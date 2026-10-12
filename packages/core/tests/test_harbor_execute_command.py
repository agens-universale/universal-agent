import json

from gc_core.backends.protocol import ExecResult, bound_exec_result
from gc_core.config_modules.agent import DEFAULT_EXEC_TIMEOUT_SEC
from gc_core.tools.exec.execute_command import ExecuteCommand
from gc_testkit.harbor_fakes import bind_os_backend
from gc_testkit.helpers.tool_ctx import tool_settings


class _RecordingFakeBackend:
    """Records exec()/is_dir() calls; container semantics (no host cwd default)."""

    def __init__(
        self,
        stdout="",
        stderr="",
        rc=0,
        dirs=None,
        timed_out=False,
        capture_max_chars=None,
    ):
        res = ExecResult(
            stdout=stdout, stderr=stderr, return_code=rc, timed_out=timed_out
        )
        self.res = (
            res
            if capture_max_chars is None
            else bound_exec_result(res, capture_max_chars)
        )
        self.calls = []
        self._dirs = set(dirs or [])

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.calls.append(
            {"command": command, "cwd": cwd, "timeout": timeout, "env": env}
        )
        return self.res

    async def is_dir(self, path):
        return path in self._dirs

    def default_cwd(self):
        return None  # container: no implicit host cwd


def _tool(backend, config=None):
    t = ExecuteCommand.__new__(ExecuteCommand)
    # Описание теста — словарь: гарды, не заданные тестом, выключены
    # (``tool_settings``), как и прежде при плоском словаре.
    return bind_os_backend(t, backend, settings=tool_settings(config))


def test_envelope_shape_matches_base_contract():
    b = _RecordingFakeBackend(stdout="hi", rc=0)
    env = json.loads(_tool(b)(command="echo hi"))
    # Same fields the base prompt_description advertises:
    for k in (
        "tool",
        "ok",
        "command",
        "cwd",
        "exit_code",
        "timed_out",
        "error",
        "stdout",
        "stderr",
    ):
        assert k in env, k
    assert "truncated" not in env
    assert (
        env["exit_code"] == 0
        and env["ok"] is True
        and env["timed_out"] is False
    )


def test_default_timeout_flows_from_exec_timeout_sec_config():
    """When timeout is omitted, agent.exec_timeout_sec from config is used."""
    b = _RecordingFakeBackend()
    _tool(b, config={"agent.exec_timeout_sec": 600})(command="x")
    assert b.calls[0]["timeout"] == 600


def test_default_timeout_without_config_is_schema_default():
    b = _RecordingFakeBackend()
    _tool(b)(command="x")
    assert b.calls[0]["timeout"] == DEFAULT_EXEC_TIMEOUT_SEC


def test_explicit_timeout_forwarded():
    b = _RecordingFakeBackend()
    _tool(b)(command="x", timeout=90)
    assert b.calls[0]["timeout"] == 90


def test_output_truncated_by_capture_cap():
    b = _RecordingFakeBackend(stdout="A" * 10000, capture_max_chars=100)
    env = json.loads(
        _tool(
            b,
            config={
                "agent.exec_output_capture_max_chars": 100,
                "agent.exec_output_preview_edge_chars": 10,
            },
        )(command="x")
    )
    assert env["truncated"] is True
    assert len(env["stdout"]["head"]) == 50
    assert len(env["stdout"]["tail"]) == 50
    assert env["stdout"]["total_chars"] == 10000
    assert env["stdout"]["omitted_chars"] == 9900
    assert env["preview"]["stdout"]["head"] == "A" * 10


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
    env = json.loads(_tool(b)(command="x", timeout=1))
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
    _tool(b)(
        command="printenv FOO", env_vars=[{"name": "FOO", "value": "bar"}]
    )
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


# --- Gap 2: timeout and max_output_chars validation ---


def test_timeout_zero_returns_invalid_argument():
    """timeout=0 must return invalid_argument with field='timeout'."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", timeout=0))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout"
    assert len(b.calls) == 0


def test_timeout_negative_returns_invalid_argument():
    """timeout<0 must return invalid_argument."""
    b = _RecordingFakeBackend()
    env = json.loads(_tool(b)(command="x", timeout=-5))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "timeout"


def test_max_output_chars_argument_no_longer_exists():
    """Скрытого второго лимита нет: аргумент игнорируется, команда исполняется."""
    b = _RecordingFakeBackend(stdout="hi")
    env = json.loads(_tool(b)(command="x", max_output_chars=0))
    assert env["ok"] is True
    assert env["stdout"]["content"] == "hi"
    assert len(b.calls) == 1


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
