"""Harbor walk B1: private 50M channel, fail-closed on ceiling hit."""
import asyncio
import threading

import pytest

from gc_core.backends.harbor import HarborBackend
from gc_core.backends.protocol import (
    DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS,
    WALK_OUTPUT_MAX_CHARS,
    WalkOutputTooLargeError,
)


class FakeExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False):
        self.stdout = stdout
        self.stderr = stderr
        self.return_code = return_code
        self.timed_out = timed_out


class FakeEnv:
    def __init__(self):
        self.calls = []

    async def exec(self, command, **kw):
        self.calls.append(command)
        return FakeExecResult(stdout="", return_code=0)

    async def is_file(self, path, user=None):
        return False

    async def is_dir(self, path, user=None):
        return True


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_walk_uses_elevated_cap_not_tool_60k(loop_in_thread):
    env = FakeEnv()
    # Exceed tool 60k but stay under walk 50M.
    lines = [f"dir/sub/f{i:05d}_long_name_padding.txt\tf" for i in range(2500)]
    body = "\n".join(lines) + "\n"
    assert len(body) > DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert len(body) < WALK_OUTPUT_MAX_CHARS

    async def big_walk(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout=body, return_code=0)

    env.exec = big_walk
    backend = HarborBackend(env, loop_in_thread)
    rows = backend.walk("/work")
    assert len(rows) == 2500
    assert rows[0][0].startswith("dir/sub/f00000")
    assert rows[0][1] is False
    # tool exec still 60k
    env.calls.clear()

    async def huge_tool(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout="Y" * 100_000, return_code=0)

    env.exec = huge_tool
    res = backend.exec("echo y")
    assert len(res.stdout) == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert res.stdout_hit_capture_cap is True


def test_walk_hit_ceiling_raises(loop_in_thread):
    env = FakeEnv()
    ceiling = 1000
    body = "a" * (ceiling + 50)

    async def over(command, **kw):
        return FakeExecResult(stdout=body, return_code=0)

    env.exec = over
    backend = HarborBackend(
        env, loop_in_thread, walk_output_max_chars=ceiling
    )
    with pytest.raises(WalkOutputTooLargeError) as ei:
        backend.walk("/work")
    assert ei.value.ceiling_chars == ceiling
    assert "/work" in str(ei.value)


def test_listdir_hit_ceiling_raises(loop_in_thread):
    env = FakeEnv()
    ceiling = 500
    body = "x\tf\n" * 200

    async def over(command, **kw):
        return FakeExecResult(stdout=body, return_code=0)

    env.exec = over
    backend = HarborBackend(
        env, loop_in_thread, walk_output_max_chars=ceiling
    )
    with pytest.raises(WalkOutputTooLargeError):
        backend.listdir("/work")


def test_walk_parse_ok_under_ceiling(loop_in_thread):
    env = FakeEnv()

    async def ok(command, **kw):
        return FakeExecResult(stdout="src\td\nsrc/a.py\tf\n", return_code=0)

    env.exec = ok
    backend = HarborBackend(env, loop_in_thread)
    assert backend.walk("/work") == [("src", True), ("src/a.py", False)]
