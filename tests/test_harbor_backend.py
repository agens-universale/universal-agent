import asyncio
import threading
import pytest
from backends.harbor import HarborBackend
from backends.protocol import FsBackend


class FakeExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code
        self.timed_out = timed_out


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

    async def is_file(self, path, user=None):
        return path.endswith(".txt")

    async def is_dir(self, path, user=None):
        return path.endswith("/")


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


def test_is_file_is_dir_use_native_env_handles(loop_in_thread):
    # is_file/is_dir must delegate to Harbor's BaseEnvironment, not shell `test`.
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    assert backend.is_file("/work/a.txt") is True
    assert backend.is_file("/work/dir") is False
    assert backend.is_dir("/work/dir/") is True
    assert backend.is_dir("/work/a.txt") is False
    # delegation goes through env.is_file/is_dir, NOT exec → no shell `test` calls
    assert not any("test -" in c for c in env.calls)


def test_is_sandbox_and_default_exec_timeout(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    assert backend.is_sandbox is True
    # Timeout comes from agent.exec_timeout_sec; backend does not hardcode one.
    assert backend.default_exec_timeout is None


def test_implements_full_fsbackend_protocol(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    assert isinstance(backend, FsBackend)


def test_listdir_parses_find_output(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "b\td\na\tf\n")
    backend = HarborBackend(env, loop_in_thread)
    result = backend.listdir("/work")
    assert result == [("a", False), ("b", True)]


def test_listdir_raises_file_not_found_on_nonzero_rc(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "", return_code=1)
    backend = HarborBackend(env, loop_in_thread)
    with pytest.raises(FileNotFoundError):
        backend.listdir("/missing")


def test_append_creates_file_if_missing(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    # exists() -> exec("test -e ...") returns rc 0 by default FakeEnv.exec, so
    # simulate missing file via a custom exec that fails "test -e" but succeeds otherwise.
    async def fake_exec(command, **kw):
        env.calls.append(command)
        if command.startswith("test -e"):
            return FakeExecResult(stdout="", return_code=1)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    env.exec = fake_exec
    backend.append("/work/new.txt", "hello")
    assert env.uploaded and env.uploaded[0][1] == "/work/new.txt"


def test_append_concatenates_existing_content(loop_in_thread):
    env = FakeEnv()
    uploaded_contents = []

    async def fake_exec(command, **kw):
        env.calls.append(command)
        if command.startswith("test -e"):
            return FakeExecResult(stdout="", return_code=0)
        if command.startswith("cat "):
            return FakeExecResult(stdout="existing-", return_code=0)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def fake_upload_file(src, dst):
        from pathlib import Path
        uploaded_contents.append(Path(src).read_text(encoding="utf-8"))
        env.uploaded.append((str(src), dst))

    env.exec = fake_exec
    env.upload_file = fake_upload_file
    backend = HarborBackend(env, loop_in_thread)
    backend.append("/work/f.txt", "more")
    assert uploaded_contents == ["existing-more"]


def test_exec_env_prefix_quoting(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    backend.exec("echo hi", env={"FOO": "bar baz"})
    assert env.calls == ["env FOO='bar baz' echo hi"]


def test_exec_env_prefix_with_cwd(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    backend.exec("echo hi", cwd="/work", env={"FOO": "bar"})
    assert env.calls == ["cd /work && env FOO=bar echo hi"]


def test_every_impl_signature_accepts_protocol_parameters():
    # ABC registration only checks method NAMES; this guards signatures.
    # For each abstract method, a protocol-shaped call (keyword-capable params
    # passed by name — catches renamed/missing params like write() without
    # `encoding`) must bind onto the HarborBackend implementation.
    import inspect

    for name in sorted(FsBackend.__abstractmethods__):
        proto = inspect.signature(getattr(FsBackend, name))
        impl = inspect.signature(getattr(HarborBackend, name))
        args, kwargs = [None], {}  # leading None stands for self
        for p in list(proto.parameters.values())[1:]:
            if p.kind == p.POSITIONAL_ONLY:
                args.append(None)
            else:
                kwargs[p.name] = None
        try:
            impl.bind(*args, **kwargs)
        except TypeError as e:
            pytest.fail(f"HarborBackend.{name}{impl} rejects protocol call {proto}: {e}")


def test_write_and_append_accept_encoding_kwarg(loop_in_thread):
    # Regression guard: unified fs tools call backend.write(..., encoding=...)
    # (e.g. fs_write_file); must not TypeError against the container backend.
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    backend.write("/work/a.txt", "data", encoding="utf-8")
    backend.append("/work/a.txt", "more", encoding="utf-8")
    assert env.uploaded and env.uploaded[0][1] == "/work/a.txt"


def _make_exec_returning(env, stdout, return_code=0):
    async def fake_exec(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout=stdout, return_code=return_code)

    return fake_exec


def test_exec_propagates_timed_out_from_environment(loop_in_thread):
    env = FakeEnv()

    async def timed_exec(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout="partial", stderr="", return_code=None, timed_out=True)

    env.exec = timed_exec
    backend = HarborBackend(env, loop_in_thread)
    result = backend.exec("sleep 99", timeout=1)
    assert result.timed_out is True
    assert result.return_code is None
    assert result.stdout == "partial"


def test_read_head_bytes_uses_head_not_full_file(loop_in_thread):
    env = FakeEnv()

    async def head_exec(command, **kw):
        env.calls.append(command)
        if command.startswith("head -c"):
            return FakeExecResult(stdout="hel", return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = backend.read_head_bytes("/work/a.bin", 3)
    assert sample == b"hel"
    assert any(c.startswith("head -c 3 ") for c in env.calls)
