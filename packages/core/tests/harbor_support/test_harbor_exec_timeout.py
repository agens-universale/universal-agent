"""HarborBackend maps Harbor exec timeouts to ExecResult(timed_out=True) and keeps
the output streamed before the timeout (spec 2026-09-06 harbor-exec-timeout-partial-output).
"""
import asyncio
import contextlib
import contextvars
import json
import threading

import pytest

from gc_testkit.harbor_fakes import bind_os_backend
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


class StreamingEnv(RaisingEnv):
    """Fake Harbor env with ``scoped_output_callback``: streams ``chunks`` to the
    scoped callback (like Docker ``_collect_streamed_output``), then raises ``exc``
    or returns ``result``.

    The callback lives in a ``ContextVar`` exactly like ``BaseEnvironment``: the
    scope is entered in the caller's thread while ``exec`` runs as a task on the
    adapter loop, so this fake fails if the context does not reach the coroutine.
    """

    def __init__(self, exc=None, chunks=(), result=None):
        super().__init__(exc)
        self.chunks = list(chunks)
        self.result = result
        self._callbacks = contextvars.ContextVar("callbacks", default=())

    @property
    def active_callbacks(self):
        return list(self._callbacks.get())

    @contextlib.contextmanager
    def scoped_output_callback(self, callback):
        token = self._callbacks.set((*self._callbacks.get(), callback))
        try:
            yield
        finally:
            self._callbacks.reset(token)

    async def exec(self, command, **kw):
        self.calls.append(command)
        for text, stream in self.chunks:
            for cb in self._callbacks.get():
                await cb(text, stream)
        if self.exc is not None:
            raise self.exc
        return self.result


class _Res:
    def __init__(self, stdout, stderr=None, return_code=0):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code


TIMEOUT = RuntimeError("Command timed out after 10 seconds")


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


def test_harbor_backend_timeout_keeps_streamed_chunks(loop_in_thread):
    """Timeout = partial completion: chunks streamed before the timeout survive."""
    env = StreamingEnv(TIMEOUT, chunks=[("line 1\n", "stdout"), ("line 2\n", "stdout"), ("warn\n", "stderr")])

    result = HarborBackend(env, loop_in_thread).exec("make test", timeout=10)

    assert result.timed_out is True
    assert result.return_code is None
    assert result.stdout == "line 1\nline 2\n"
    assert result.stderr == "warn\n"


def test_harbor_backend_timeout_without_chunks_is_empty(loop_in_thread):
    """No chunks before timeout → empty streams are legal (silent command)."""
    result = HarborBackend(StreamingEnv(TIMEOUT), loop_in_thread).exec("sleep 99", timeout=10)

    assert result.timed_out is True
    assert result.stdout == ""
    assert result.stderr == ""


def test_harbor_backend_timeout_ignores_exception_attrs(loop_in_thread):
    """Streamed chunks are the source of truth, not ``exc.stdout``."""
    exc = RuntimeError("Command timed out after 10 seconds")
    exc.stdout = "from exception"
    env = StreamingEnv(exc, chunks=[("from stream\n", "stdout")])

    result = HarborBackend(env, loop_in_thread).exec("make", timeout=10)

    assert result.stdout == "from stream\n"


def test_harbor_backend_timeout_partial_output_bounded_by_common_cap(loop_in_thread):
    """Partial output keeps real head+tail bounded by the same capture cap."""
    env = StreamingEnv(TIMEOUT, chunks=[("x" * 50, "stdout"), ("y" * 50, "stdout")])

    result = HarborBackend(env, loop_in_thread, exec_output_capture_max_chars=70).exec("cmd", timeout=10)

    assert result.timed_out is True
    cs = result.stdout_stream
    assert cs.truncated is True
    assert cs.total_chars == 100
    assert cs.head == "x" * 35
    assert cs.tail == "y" * 35
    assert cs.omitted_chars == 30


def test_harbor_backend_timeout_output_exactly_at_cap_then_more_keeps_real_tail(loop_in_thread):
    """Chunks after the head is full still count and still update the ring tail."""
    env = StreamingEnv(TIMEOUT, chunks=[("x" * 70, "stdout"), ("y", "stdout")])

    result = HarborBackend(env, loop_in_thread, exec_output_capture_max_chars=70).exec("cmd", timeout=10)

    cs = result.stdout_stream
    assert cs.truncated is True
    assert cs.total_chars == 71
    assert cs.tail.endswith("y")
    assert result.stdout_hit_capture_cap is True


def test_harbor_backend_timeout_output_exactly_at_cap_without_more_is_not_flagged(loop_in_thread):
    env = StreamingEnv(TIMEOUT, chunks=[("x" * 70, "stdout")])

    result = HarborBackend(env, loop_in_thread, exec_output_capture_max_chars=70).exec("cmd", timeout=10)

    assert result.stdout == "x" * 70
    assert result.stdout_hit_capture_cap is False


def test_harbor_backend_timeout_respects_per_call_cap(loop_in_thread):
    env = StreamingEnv(TIMEOUT, chunks=[("x" * 100, "stdout")])

    result = HarborBackend(env, loop_in_thread)._exec("cmd", timeout=10, capture_max_chars=30)

    assert result.stdout == "x" * 30
    assert result.stdout_hit_capture_cap is True


def test_harbor_backend_success_uses_result_and_releases_callback(loop_in_thread):
    """Success path: ExecResult from ``res``; callback scope is left afterwards."""
    env = StreamingEnv(chunks=[("streamed\n", "stdout")], result=_Res("final out", "final err", 3))

    result = HarborBackend(env, loop_in_thread).exec("cmd", timeout=10)

    assert (result.stdout, result.stderr, result.return_code, result.timed_out) == ("final out", "final err", 3, False)
    assert env.active_callbacks == []


def test_harbor_backend_timeout_releases_callback(loop_in_thread):
    env = StreamingEnv(TIMEOUT, chunks=[("a\n", "stdout")])

    HarborBackend(env, loop_in_thread).exec("cmd", timeout=10)

    assert env.active_callbacks == []


def test_execute_command_timeout_envelope_carries_partial_output(loop_in_thread):
    """D3: the agent sees what the command printed before the timeout."""
    env = StreamingEnv(TIMEOUT, chunks=[("Length: 162M\n", "stdout"), ("Saving to: cifar.tar.gz\n", "stdout")])
    tool = ExecuteCommand.__new__(ExecuteCommand)
    tool._config_dict = None
    bind_os_backend(tool, HarborBackend(env, loop_in_thread), config_dict={})

    envelope = json.loads(tool(command="wget …", timeout=10))

    assert envelope["timed_out"] is True
    assert envelope["error"]["type"] == "timeout"
    assert "Length: 162M" in envelope["stdout"]["content"]
    assert "Saving to: cifar.tar.gz" in envelope["stdout"]["content"]


def test_execute_command_maps_harbor_timeout_envelope(loop_in_thread):
    """Contract: Harbor timeout → timed_out=true, error.type=timeout (not execution_failed)."""
    backend = _backend(
        RuntimeError("Command timed out after 300 seconds"),
        loop_in_thread,
    )
    tool = ExecuteCommand.__new__(ExecuteCommand)
    tool._config_dict = None
    bind_os_backend(tool, backend, config_dict={})

    env = json.loads(tool(command="sleep 999", timeout=300))

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

    env = json.loads(tool(command="true", timeout=30))

    assert env["ok"] is False
    assert env["timed_out"] is False
    assert env["exit_code"] == -1
    assert env["error"]["type"] == "execution_failed"
