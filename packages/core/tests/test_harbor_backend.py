import asyncio
import threading
import pytest
from gc_core.backends.harbor import HarborBackend
from gc_core.backends.protocol import FsBackend


class FakeExecResult:
    def __init__(self, stdout="", stderr="", return_code=0, timed_out=False):
        self.stdout, self.stderr, self.return_code = stdout, stderr, return_code
        self.timed_out = timed_out


class FakeEnv:
    def __init__(self):
        self.calls = []
        self.uploaded = []
        self.uploaded_bytes = []
        self.downloaded = []

    async def exec(self, command, **kw):
        self.calls.append(command)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def upload_file(self, src, dst):
        from pathlib import Path
        data = Path(src).read_bytes()
        self.uploaded.append((str(src), dst))
        self.uploaded_bytes.append(data)

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
    assert backend.exec_output_capture_max_chars == 60_000


def test_exec_bounds_large_stdout_to_capture_cap(loop_in_thread):
    """D8: Harbor ExecResult on agent is capped; hit_capture_cap set."""
    env = FakeEnv()
    huge = "X" * 1000

    async def big_exec(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout=huge, return_code=0)

    env.exec = big_exec
    backend = HarborBackend(env, loop_in_thread)
    backend.exec_output_capture_max_chars = 100
    result = backend.exec("echo huge")
    assert len(result.stdout) == 100
    assert result.stdout_hit_capture_cap is True
    assert result.stderr_hit_capture_cap is False
    assert result.return_code == 0


def test_exec_per_call_capture_max_overrides_tool_cap(loop_in_thread):
    """Internal post-processed exec (native grep) may raise the RAM cap."""
    env = FakeEnv()
    huge = "X" * 1000

    async def big_exec(command, **kw):
        return FakeExecResult(stdout=huge, return_code=0)

    env.exec = big_exec
    backend = HarborBackend(env, loop_in_thread)
    backend.exec_output_capture_max_chars = 100
    result = backend.exec("echo huge", capture_max_chars=500)
    assert len(result.stdout) == 500
    assert result.stdout_hit_capture_cap is True


def test_exec_small_output_no_cap_hit(loop_in_thread):
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    result = backend.exec("echo hi")
    assert result.stdout_hit_capture_cap is False
    assert result.stderr_hit_capture_cap is False


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
    assert env.uploaded and ".gc-append-tmp." in env.uploaded[0][1]
    assert env.uploaded[0][1].startswith("/work/new.txt.gc-append-tmp.")
    assert any(">>" in c and "/work/new.txt" in c for c in env.calls)

def test_append_concatenates_existing_content(loop_in_thread):
    env = FakeEnv()
    uploaded_payloads = []

    async def fake_exec(command, **kw):
        env.calls.append(command)
        return FakeExecResult(stdout=f"ran:{command}", return_code=0)

    async def fake_upload_file(src, dst):
        from pathlib import Path
        uploaded_payloads.append((dst, Path(src).read_bytes()))
        env.uploaded.append((str(src), dst))

    env.exec = fake_exec
    env.upload_file = fake_upload_file
    backend = HarborBackend(env, loop_in_thread)
    backend.append("/work/f.txt", "more")
    assert len(uploaded_payloads) == 1
    remote_tmp, payload = uploaded_payloads[0]
    assert remote_tmp.startswith("/work/f.txt.gc-append-tmp.")
    assert ".gc-append-tmp." in remote_tmp
    assert payload == b"more"
    assert any("cat " in c and ">>" in c and "/work/f.txt" in c for c in env.calls)
    assert not any(c.startswith("test -e") for c in env.calls)

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
    assert any(dst == "/work/a.txt" for _, dst in env.uploaded)
    assert any(".gc-append-tmp." in dst for _, dst in env.uploaded)

def test_harbor_write_bytes_preserves_cp1251(loop_in_thread):
    """Harbor text-write must encode on host and upload raw bytes (not force utf-8)."""
    env = FakeEnv()
    backend = HarborBackend(env, loop_in_thread)
    text = "Привет"
    backend.write("/work/ru.txt", text, encoding="cp1251")
    assert env.uploaded_bytes[-1] == text.encode("cp1251")
    assert env.uploaded_bytes[-1] != text.encode("utf-8")


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
    import base64

    env = FakeEnv()
    raw = b"hel"

    async def head_exec(command, **kw):
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            return FakeExecResult(
                stdout=base64.b64encode(raw).decode("ascii"),
                return_code=0,
            )
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = backend.read_head_bytes("/work/a.bin", 3)
    assert sample == raw
    assert any(
        c.startswith("head -c 3 ") and "| base64" in c for c in env.calls
    )


def test_read_head_bytes_append_sample_not_cut_by_tool_60k(loop_in_thread):
    """fs_append samples 100KiB; base64 (~137k) exceeds tool-exec 60k."""
    import base64

    from gc_core.backends.protocol import DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    from gc_core.utils.file_text import APPEND_HEAD_SAMPLE_BYTES

    env = FakeEnv()
    raw = b"A" * APPEND_HEAD_SAMPLE_BYTES
    encoded = base64.b64encode(raw).decode("ascii")
    assert len(encoded) > DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS

    async def head_exec(command, **kw):
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            return FakeExecResult(stdout=encoded, return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = backend.read_head_bytes("/work/big.txt", APPEND_HEAD_SAMPLE_BYTES)
    assert sample == raw

    async def huge_tool(command, **kw):
        return FakeExecResult(stdout="Y" * 100_000, return_code=0)

    env.exec = huge_tool
    tool = backend.exec("echo y")
    assert len(tool.stdout) == DEFAULT_EXEC_OUTPUT_CAPTURE_MAX_CHARS
    assert tool.stdout_hit_capture_cap is True


def test_read_head_bytes_raises_if_capture_ceiling_hit(loop_in_thread):
    import base64

    env = FakeEnv()
    raw = b"B" * 200
    encoded = base64.b64encode(raw).decode("ascii")

    async def head_exec(command, **kw):
        if "head -c" in command and "base64" in command:
            return FakeExecResult(stdout=encoded, return_code=0)
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread, walk_output_max_chars=50)
    with pytest.raises(OSError, match="capture ceiling"):
        backend.read_head_bytes("/work/a.bin", 200)


def test_read_head_bytes_preserves_non_utf8_bytes(loop_in_thread):
    """D: raw bytes must survive exec (no utf-8/surrogateescape mangling)."""
    import base64

    env = FakeEnv()
    # High bytes + NUL — would break under encode("utf-8") of a text stdout.
    raw = bytes([0xCF, 0xF0, 0xE8, 0xE2, 0xE5, 0xF2, 0x00, 0xFF])

    async def head_exec(command, **kw):
        env.calls.append(command)
        if "head -c" in command and "base64" in command:
            return FakeExecResult(
                stdout=base64.b64encode(raw).decode("ascii"),
                return_code=0,
            )
        return FakeExecResult(stdout="", return_code=1)

    env.exec = head_exec
    backend = HarborBackend(env, loop_in_thread)
    sample = backend.read_head_bytes("/work/legacy.txt", len(raw))
    assert sample == raw
    assert b"\x00" in sample
    assert sample[0] == 0xCF


# --- walk_entries: prune внутри find, размер из -printf %s ----------------------

def test_walk_entries_prunes_in_find_and_parses_sizes(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "src\td\t4096\nsrc/a.py\tf\t12\n")
    backend = HarborBackend(env, loop_in_thread)

    entries = backend.walk_entries("/work", prune_names=("node_modules",), prune_dot_dirs=True)

    assert [(e.rel, e.is_dir, e.size) for e in entries] == [("src", True, None), ("src/a.py", False, 12)]
    cmd = env.calls[-1]
    # -H: паритет с os.walk по симлинк-вершине; -printf ДО -prune: сам каталог в листинге остаётся.
    assert cmd.startswith("find -H /work -mindepth 1 '(' -type d -name node_modules -o -type d -name '.*' ')' -printf")
    assert " -prune -o -printf" in cmd
    assert cmd.endswith(r"-printf '%P\t%y\t%s\n'")


def test_walk_keeps_tuples_and_never_prunes(loop_in_thread):
    env = FakeEnv()
    env.exec = _make_exec_returning(env, "src\td\t4096\nsrc/a.py\tf\t12\n")
    backend = HarborBackend(env, loop_in_thread)

    assert backend.walk("/work") == [("src", True), ("src/a.py", False)]
    assert "-prune" not in env.calls[-1]


def test_walk_entries_hit_ceiling_raises(loop_in_thread):
    from gc_core.backends.protocol import WalkOutputTooLargeError

    env = FakeEnv()
    env.exec = _make_exec_returning(env, "x\tf\t1\n" * 200)
    backend = HarborBackend(env, loop_in_thread, walk_output_max_chars=500)
    with pytest.raises(WalkOutputTooLargeError):
        backend.walk_entries("/work")


def test_normpath_collapses_dot_and_double_slash(loop_in_thread):
    """find печатает пути префиксом вызова; prune сравнивает строки —
    rstrip-нормализация пропускала «/проект//x/./y»."""
    backend = HarborBackend(FakeEnv(), loop_in_thread)
    assert backend.normpath("/w//x/./y/") == "/w/x/y"
    assert backend.normpath("/w/a/../b") == "/w/b"
    assert backend.normpath("/") == "/"
