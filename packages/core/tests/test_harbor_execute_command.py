import json

from gc_core.backends.protocol import ExecResult, bound_exec_result
from gc_core.tools.exec.execute_command import (
    ExecuteCommand,
    _probe_came_back,
    probe_total_in_command,
)
from gc_testkit.harbor_fakes import bind_os_backend


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
        self.is_dir_calls = []
        self._dirs = set(dirs or [])
        self._default_cwd = None
        self.is_sandbox = False
        # Живая проба: echo $((a+b)) возвращает сумму своей строкой.
        # Мёртвая — тот же отказ, что и у команды (демон не достучался).
        self.probe_alive = True

    def exec(self, command, *, cwd=None, timeout=None, env=None):
        self.calls.append(
            {"command": command, "cwd": cwd, "timeout": timeout, "env": env}
        )
        total = probe_total_in_command(command)
        if self.is_sandbox and total is not None:
            if not self.probe_alive:
                return ExecResult(
                    stdout=self.res.stdout,
                    stderr=self.res.stderr,
                    return_code=1,
                )
            return ExecResult(
                stdout=f"profile banner\n{total}\n", return_code=0
            )
        return self.res

    async def is_dir(self, path):
        self.is_dir_calls.append(path)
        return path in self._dirs

    def default_cwd(self):
        return self._default_cwd


def _tool(backend, config_dict=None):
    t = ExecuteCommand.__new__(ExecuteCommand)
    cfg = config_dict if config_dict is not None else {}
    t._config_dict = cfg
    return bind_os_backend(t, backend, config_dict=cfg)


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
    _tool(b, config_dict={"exec_timeout_sec": 600})(command="x")
    assert b.calls[0]["timeout"] == 600


def test_default_timeout_without_config_is_schema_default():
    b = _RecordingFakeBackend()
    _tool(b)(command="x")
    assert b.calls[0]["timeout"] == 120


def test_explicit_timeout_forwarded():
    b = _RecordingFakeBackend()
    _tool(b)(command="x", timeout=90)
    assert b.calls[0]["timeout"] == 90


def test_output_truncated_by_capture_cap():
    b = _RecordingFakeBackend(stdout="A" * 10000, capture_max_chars=100)
    env = json.loads(
        _tool(
            b,
            config_dict={
                "exec_output_capture_max_chars": 100,
                "exec_output_preview_edge_chars": 10,
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
    assert b.is_dir_calls == ["/workspace"]


def test_primed_default_cwd_skips_is_dir_on_harbor_and_remote():
    """WORKDIR / remote pwd уже прогрет: мёртвый is_dir не должен врать про cwd.

    Harbor сводит упавший exec к False («Not a directory»). RemoteShell
    перед командой делает тот же `test -d`. Оба бэкенда отдают путь через
    `default_cwd()`, и для него проверка пропускается — ошибка транспорта
    приходит уже из самого запуска.
    """
    b = _RecordingFakeBackend(
        stderr="Cannot connect to the Docker daemon at tcp://localhost:2375",
        rc=1,
    )
    b._default_cwd = "/app"
    b.is_sandbox = True
    b.probe_alive = False
    env = json.loads(_tool(b)(command="echo test"))
    assert b.is_dir_calls == []
    assert b.calls[0]["cwd"] == "/app"
    assert probe_total_in_command(b.calls[0]["command"]) is not None
    assert len(b.calls) == 1
    assert env["ok"] is False
    assert env["command"] == "echo test"
    assert env["error"]["type"] == "non_zero_exit"
    assert "Docker daemon" in env["stderr"]["content"]
    assert env["error"]["message"].startswith("Runtime connection failed.")
    assert "Wait a few seconds and retry" in env["error"]["message"]
    assert "turn_finish" in env["error"]["message"]
    assert "tcp://localhost:2375" in env["error"]["message"]


def test_live_probe_then_runs_the_command():
    b = _RecordingFakeBackend(stdout="ok", rc=0)
    b.is_sandbox = True
    b._default_cwd = "/work"
    env = json.loads(_tool(b)(command="echo test"))
    assert probe_total_in_command(b.calls[0]["command"]) is not None
    assert b.calls[1]["command"] == "echo test"
    assert env["ok"] is True
    assert env["stdout"]["content"] == "ok"


def test_probe_accepts_sum_line_among_extra_output():
    res = ExecResult(stdout="banner\n46\n", stderr="warning\n", return_code=0)
    assert _probe_came_back(res, 46) is True


def test_probe_rejects_echoed_command_text():
    """Цитата команды содержит слагаемые, но не сумму отдельной строкой."""
    res = ExecResult(
        stdout="command echo $((12+34)) was not executed\n",
        return_code=0,
    )
    assert _probe_came_back(res, 46) is False


def test_command_failure_keeps_status_message():
    """Обычный ненулевой код команды — не обрыв транспорта."""
    b = _RecordingFakeBackend(stderr="boom", rc=2)
    env = json.loads(_tool(b)(command="x"))
    assert env["error"]["message"] == "Command exited with status 2"


def test_harbor_daemon_text_in_stdout_is_a_connection_failure():
    """Compose сливает stderr клиента в stdout пробы."""
    b = _RecordingFakeBackend(
        stdout=(
            "Cannot connect to the Docker daemon at tcp://localhost:2375. "
            "Is the docker daemon running?\n"
        ),
        rc=1,
    )
    b.is_sandbox = True
    b.probe_alive = False
    env = json.loads(_tool(b)(command="echo test"))
    assert env["error"]["type"] == "non_zero_exit"
    assert env["error"]["message"].startswith("Runtime connection failed.")
    assert "Docker daemon" in env["stdout"]["content"]
    assert any(
        probe_total_in_command(c["command"]) is not None for c in b.calls
    )
    assert all(c["command"] != "echo test" for c in b.calls)


def test_explicit_cwd_equal_to_primed_default_also_skips_is_dir():
    b = _RecordingFakeBackend(stdout="ok", rc=0)
    b._default_cwd = "/work"
    env = json.loads(_tool(b)(command="echo test", cwd="/work"))
    assert env["ok"] is True
    assert b.is_dir_calls == []
    assert b.calls[0]["cwd"] == "/work"


def test_explicit_cwd_other_than_default_is_still_checked():
    b = _RecordingFakeBackend(dirs=set())
    b._default_cwd = "/app"
    env = json.loads(_tool(b)(command="echo test", cwd="/tmp/missing"))
    assert env["ok"] is False
    assert env["error"]["type"] == "invalid_argument"
    assert env["error"]["field"] == "cwd"
    assert env["error"]["message"] == "Not a directory"
    assert b.is_dir_calls == ["/tmp/missing"]
    assert b.calls == []


def test_explicit_cwd_transport_failure_is_runtime_down():
    """Мёртвый Docker на чужом cwd — не «Not a directory», а тот же надж."""
    b = _RecordingFakeBackend()
    b._default_cwd = "/app"
    b.is_sandbox = True

    async def dead_is_dir(path):
        b.is_dir_calls.append(path)
        raise OSError(
            "Cannot connect to the Docker daemon at tcp://localhost:2375"
        )

    b.is_dir = dead_is_dir
    env = json.loads(_tool(b)(command="echo test", cwd="/tmp/other"))
    assert env["ok"] is False
    assert env["error"]["type"] == "execution_failed"
    assert "field" not in env["error"]
    assert "Not a directory" not in env["error"]["message"]
    assert env["error"]["message"].startswith("Runtime connection failed.")
    assert "turn_finish" in env["error"]["message"]
    assert "Docker daemon" in env["error"]["message"]
    assert b.calls == []
    assert b.is_dir_calls == ["/tmp/other"]
