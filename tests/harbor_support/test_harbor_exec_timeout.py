"""HarborBackend maps Harbor exec timeouts to ExecResult(timed_out=True).

See docs_md/improvments_gleb_090726/05-harbor-exec-timeout.md.
"""
import asyncio
import json
import threading

import pytest

from tests.harbor_support.fakes import bind_os_backend
from gc_core.backends.harbor import HarborBackend
from gc_core.tools.exec.execute_command import ExecuteCommand


class RaisingEnv:
    """Fake Harbor env whose exec raises a configured exception."""

    def __init__(self, exc: BaseException):
        self.exc = exc
        self.calls = []

    async def exec(self, command, **kw):
        self.calls.append(command)
        raise self.exc


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def _backend(exc: BaseException, loop) -> HarborBackend:
    return HarborBackend(RaisingEnv(exc), loop)


def test_harbor_backend_timeout_from_runtime_error(loop_in_thread):
    """Harbor Docker/Apple: RuntimeError with timeout message → timed_out=True."""
    result = _backend(
        RuntimeError("Command timed out after 300 seconds"),
        loop_in_thread,
    ).exec("true", timeout=300)

    assert result.timed_out is True
    assert result.return_code is None
    assert result.stdout == ""
    assert result.stderr == ""


def test_harbor_backend_timeout_from_asyncio_timeout_error(loop_in_thread):
    """Bare TimeoutError / asyncio.TimeoutError also maps to timed_out=True."""
    result = _backend(asyncio.TimeoutError(), loop_in_thread).exec("true", timeout=300)

    assert result.timed_out is True
    assert result.return_code is None


def test_harbor_backend_propagates_non_timeout_runtime_error(loop_in_thread):
    """Other RuntimeError is not masked as timeout."""
    with pytest.raises(RuntimeError, match="Failed to build Docker image"):
        _backend(
            RuntimeError("Failed to build Docker image"),
            loop_in_thread,
        ).exec("true")


def test_harbor_backend_keeps_partial_streams_from_exception(loop_in_thread):
    """If the exception carries stdout/stderr attrs, preserve them."""
    exc = RuntimeError("Command timed out after 10 seconds")
    exc.stdout = "partial out"
    exc.stderr = "partial err"

    result = _backend(exc, loop_in_thread).exec("make test", timeout=10)

    assert result.timed_out is True
    assert result.stdout == "partial out"
    assert result.stderr == "partial err"


def test_execute_command_maps_harbor_timeout_envelope(loop_in_thread):
    """Contract: Harbor timeout → timed_out=true, error.type=timeout (not execution_failed)."""
    backend = _backend(
        RuntimeError("Command timed out after 300 seconds"),
        loop_in_thread,
    )
    tool = ExecuteCommand.__new__(ExecuteCommand)
    tool._config_dict = None
    bind_os_backend(tool, backend, config_dict={})

    env = json.loads(tool(command="sleep 999", timeout_seconds=300))

    assert env["ok"] is False
    assert env["timed_out"] is True
    assert env["exit_code"] is None
    assert env["error"]["type"] == "timeout"
    assert "timed out" in env["error"]["message"].lower()


def test_execute_command_non_timeout_runtime_error_stays_execution_failed(loop_in_thread):
    """Non-timeout RuntimeError still becomes generic execution_failed."""
    backend = _backend(RuntimeError("Failed to build Docker image"), loop_in_thread)
    tool = ExecuteCommand.__new__(ExecuteCommand)
    tool._config_dict = None
    bind_os_backend(tool, backend, config_dict={})

    env = json.loads(tool(command="true", timeout_seconds=30))

    assert env["ok"] is False
    assert env["timed_out"] is False
    assert env["exit_code"] == -1
    assert env["error"]["type"] == "execution_failed"
