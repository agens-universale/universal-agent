import asyncio
import threading
import pytest
from tools.harbor.backend import HarborBackend


class FakeExecResult:
    def __init__(self, stdout="", stderr="", return_code=0):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code


class FakeEnv:
    def __init__(self):
        self.calls = []
        self.uploaded = []
        self.downloaded = []

    async def exec(self, command, **kw):
        self.calls.append(command)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def upload_file(self, src, dst):
        self.uploaded.append((str(src), dst))

    async def download_file(self, src, dst):
        from pathlib import Path
        Path(dst).write_bytes(b"old-content")
        self.downloaded.append((src, str(dst)))


@pytest.fixture
def loop_in_thread():
    loop = asyncio.new_event_loop()
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    yield loop
    loop.call_soon_threadsafe(loop.stop)
    t.join(timeout=2)


def test_exec_bridges_sync_to_async(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    result = backend.exec("echo hi")
    assert result.return_code == 0
    assert result.stdout == "ran:echo hi"
    assert env.calls == ["echo hi"]


def test_write_uses_upload(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    backend.write("/work/new.txt", "hello")
    assert env.uploaded and env.uploaded[0][1] == "/work/new.txt"


def test_edit_download_modify_upload(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    count = backend.edit("/work/f.txt", "old-content", "new-content")
    assert count == 1
    assert env.downloaded and env.uploaded
