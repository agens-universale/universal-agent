import os
import stat as stat_module
import sys

import pytest

from gc_core.run_context import create_run_context
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult, FsBackend


@pytest.fixture()
def be():
    return LocalFsBackend()


def test_implements_protocol(be):
    assert isinstance(be, FsBackend)
    assert be.is_sandbox is False
    assert be.default_exec_timeout is None


def test_stat_group(be, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hello\n", encoding="utf-8")
    p = str(f)
    assert be.exists(p) and be.lexists(p) and be.is_file(p)
    assert not be.is_dir(p) and be.is_dir(str(tmp_path))
    assert be.getsize(p) == 6
    info = be.stat_info(p, follow_symlinks=False)
    assert info["size"] == 6 and info["is_file"] and not info["is_dir"]
    assert isinstance(info["mode"], int)
    assert be.readable(p) and be.writable(p)
    assert be.realpath(p) == os.path.realpath(p)
    assert not be.is_hidden(p)
    assert be.is_hidden(str(tmp_path / ".hidden"))


def test_read_group(be, tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("l1\nl2\n", encoding="utf-8")
    p = str(f)
    assert be.read(p) == "l1\nl2\n"
    assert list(be.iter_lines(p)) == ["l1\n", "l2\n"]
    assert be.read_head_bytes(p, 2) == b"l1"
    import hashlib
    assert be.sha256_bytes(p) == hashlib.sha256(b"l1\nl2\n").hexdigest()


def test_write_group_atomic_and_append(be, tmp_path):
    p = str(tmp_path / "w.txt")
    be.write(p, "v1")
    assert be.read(p) == "v1"
    assert not os.path.exists(p + ".tmp")          # atomic: no tmp residue
    be.append(p, "-v2")
    assert be.read(p) == "v1-v2"
    bp = str(tmp_path / "raw.bin")
    be.write_bytes(bp, b"\x00\xff")
    with open(bp, "rb") as fd:
        assert fd.read() == b"\x00\xff"
    cp = str(tmp_path / "copy.bin")
    be.copy(bp, cp)
    with open(cp, "rb") as fd:
        assert fd.read() == b"\x00\xff"
    nd = str(tmp_path / "n1" / "n2")
    be.makedirs(nd)
    assert os.path.isdir(nd)


def test_read_write_cp1251_round_trip(be, tmp_path):
    p = str(tmp_path / "legacy.txt")
    text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    be.write(p, text, encoding="cp1251")
    assert open(p, "rb").read() == text.encode("cp1251")
    assert be.read(p) == text


def test_write_unsupported_encoding_raises(be, tmp_path):
    p = str(tmp_path / "x.txt")
    with pytest.raises(ValueError, match="Unsupported encoding"):
        be.write(p, "hi", encoding="ibm866")


def test_write_encode_failed_raises(be, tmp_path):
    p = str(tmp_path / "x.txt")
    with pytest.raises(ValueError, match="Cannot encode"):
        be.write(p, "Привет", encoding="latin-1")


def test_traverse_and_paths(be, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "top.md").write_text("y", encoding="utf-8")
    root = str(tmp_path)
    assert be.listdir(root) == [("sub", True), ("top.md", False)]
    assert sorted(be.walk(root)) == [("sub", True), ("sub/f.txt", False), ("top.md", False)]
    assert be.normpath(root + "/") == os.path.normpath(root + "/")
    assert be.join(root, "x") == os.path.join(root, "x")


def test_exec_ok_and_timeout(be, tmp_path):
    res = be.exec("echo hi", cwd=str(tmp_path), timeout=10)
    assert isinstance(res, ExecResult)
    assert res.return_code == 0 and res.stdout.strip() == "hi" and not res.timed_out
    res = be.exec("sleep 5", timeout=1)
    assert res.timed_out and res.return_code is None
    env_res = be.exec("echo $GC_T", timeout=10, env={"GC_T": "42"})
    assert env_res.stdout.strip() == "42"


def test_run_python(be, tmp_path):
    res = be.run_python("print('ok')", cwd=str(tmp_path), timeout=10)
    assert res.return_code == 0 and res.stdout.strip() == "ok"
    res = be.run_python("import time; time.sleep(5)", timeout=1)
    assert res.timed_out


def test_default_cwd(be):
    assert be.default_cwd() == os.getcwd()


def test_tool_backend_resolution():
    from types import SimpleNamespace

    from gc_core.tools.fs.read_file import ReadFile

    cfg = {"backend_api_mode": "chat_completions", "save_logs": False}
    ctx = create_run_context(cfg)
    t = ReadFile(ctx=ctx)
    assert isinstance(t._backend, LocalFsBackend)
    assert t._backend is ctx.os_backend

    sentinel = object()
    t2 = ReadFile.__new__(ReadFile)
    t2._ctx = SimpleNamespace(os_backend=sentinel)
    assert t2._backend is sentinel


def test_walk_symlink_to_dir_is_file_entry(be, tmp_path):
    """Symlink to a directory must be is_dir=False in walk (lstat parity with listdir)."""
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "linkdir"
    link.symlink_to(sub)
    root = str(tmp_path)
    walked = dict(be.walk(root))
    assert walked.get("linkdir") is False
    assert walked.get("sub") is True
    assert "sub/inner.txt" in walked


def test_walk_symlink_to_file(be, tmp_path):
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    walked = dict(be.walk(str(tmp_path)))
    assert walked.get("link.txt") is False


def test_write_replace_failure_cleans_tmp(be, tmp_path, monkeypatch):
    p = str(tmp_path / "target.txt")
    (tmp_path / "target.txt").write_text("keep", encoding="utf-8")
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated"):
        be.write(p, "new content")
    assert not os.path.exists(p + ".tmp")
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "keep"


def test_write_bytes_replace_failure_cleans_tmp(be, tmp_path, monkeypatch):
    p = str(tmp_path / "bin.dat")
    monkeypatch.setattr(os, "replace", lambda _s, _d: (_ for _ in ()).throw(OSError("nope")))
    with pytest.raises(OSError):
        be.write_bytes(p, b"\xff\xfe")
    assert not os.path.exists(p + ".tmp")
    assert not os.path.exists(p)


def test_read_head_bytes_binary(be, tmp_path):
    p = str(tmp_path / "raw.bin")
    data = b"hello\x00world\xff"
    with open(p, "wb") as fd:
        fd.write(data)
    assert be.read_head_bytes(p, 8) == data[:8]


# --- bounded exec runner (spec D1/D2/D9/D10) ---

def test_exec_result_has_capture_cap_flags(be):
    res = be.exec("echo hi", timeout=10)
    assert isinstance(res, ExecResult)
    assert res.stdout_hit_capture_cap is False
    assert res.stderr_hit_capture_cap is False


def test_exec_default_capture_cap_is_60k(be):
    assert be.exec_output_capture_max_chars == 60_000


def test_exec_reader_threads_are_daemon(be, monkeypatch):
    """A12: stdout/stderr readers must be daemon so mid-drain exit is not blocked."""
    import threading

    from gc_core.backends import local as local_mod

    created = []
    RealThread = threading.Thread

    def TrackingThread(*args, **kwargs):
        t = RealThread(*args, **kwargs)
        created.append(t)
        return t

    monkeypatch.setattr(local_mod.threading, "Thread", TrackingThread)
    res = be.exec("echo hi", timeout=10)
    assert res.return_code == 0
    readers = [t for t in created if t.name in ("exec-stdout-reader", "exec-stderr-reader")]
    assert len(readers) == 2
    assert all(t.daemon for t in readers)


def test_exec_bounded_stdout_not_killed(be):
    """Output exceeding cap is bounded; process is NOT killed (drain & discard)."""
    be.exec_output_capture_max_chars = 200
    # Print ~5000 chars to stdout; process must finish on its own (exit 0).
    res = be.exec("yes A | head -c 5000", timeout=15)
    assert res.return_code == 0
    assert not res.timed_out
    assert len(res.stdout) <= 200
    assert res.stdout_hit_capture_cap is True
    assert res.stderr_hit_capture_cap is False


def test_exec_bounded_stderr(be):
    be.exec_output_capture_max_chars = 150
    # Redirect to stderr only.
    res = be.exec("yes B | head -c 5000 >&2", timeout=15)
    assert res.return_code == 0
    assert len(res.stderr) <= 150
    assert res.stderr_hit_capture_cap is True
    assert res.stdout_hit_capture_cap is False


def test_exec_both_pipes_large_no_deadlock(be):
    """Both stdout and stderr large simultaneously must not deadlock the runner."""
    be.exec_output_capture_max_chars = 100
    cmd = "yes O | head -c 8000 & yes E | head -c 8000 >&2 & wait"
    res = be.exec(cmd, timeout=20)
    assert res.return_code == 0
    assert len(res.stdout) <= 100
    assert len(res.stderr) <= 100
    assert res.stdout_hit_capture_cap is True
    assert res.stderr_hit_capture_cap is True


def test_exec_small_output_no_cap_hit(be):
    be.exec_output_capture_max_chars = 60_000
    res = be.exec("echo small", timeout=10)
    assert res.return_code == 0
    assert res.stdout.strip() == "small"
    assert res.stdout_hit_capture_cap is False
    assert res.stderr_hit_capture_cap is False


def test_exec_timeout_partial_buffer(be):
    """On timeout: timed_out=True, exit_code=None, partial buffer preserved."""
    be.exec_output_capture_max_chars = 60_000
    res = be.exec("echo started; sleep 5", timeout=1)
    assert res.timed_out is True
    assert res.return_code is None
    assert "started" in res.stdout


def test_run_python_bounded_output(be):
    be.exec_output_capture_max_chars = 120
    code = "print('A' * 5000)"
    res = be.run_python(code, timeout=15)
    assert res.return_code == 0
    assert len(res.stdout) <= 120
    assert res.stdout_hit_capture_cap is True


def test_run_python_small_output_no_cap(be):
    be.exec_output_capture_max_chars = 60_000
    res = be.run_python("print('py-ok')", timeout=10)
    assert res.return_code == 0
    assert res.stdout.strip() == "py-ok"
    assert res.stdout_hit_capture_cap is False


def test_run_python_bounded_stderr(be):
    be.exec_output_capture_max_chars = 100
    code = "import sys; sys.stderr.write('E' * 4000)"
    res = be.run_python(code, timeout=15)
    assert res.return_code == 0
    assert len(res.stderr) <= 100
    assert res.stderr_hit_capture_cap is True


def test_run_python_timeout(be, tmp_path):
    res = be.run_python("import time; print('go'); time.sleep(5)", cwd=str(tmp_path), timeout=1)
    assert res.timed_out is True
    assert res.return_code is None
    assert "go" in res.stdout


def test_drain_pipe_publishes_captured_before_discard_finishes():
    """on_captured fires at hit_cap while remaining bytes are still being drained."""
    import threading
    from gc_core.backends.local import _drain_pipe

    class SlowAfterCapPipe:
        def __init__(self):
            self._chunks = [b"A" * 50, b"B" * 50]  # first fills cap=40, second drains
            self._i = 0
            self.second_started = threading.Event()
            self.release_second = threading.Event()

        def read(self, n):
            if self._i >= len(self._chunks):
                return b""
            chunk = self._chunks[self._i]
            self._i += 1
            if self._i == 2:
                self.second_started.set()
                self.release_second.wait(timeout=5)
            return chunk

        def close(self):
            pass

    pipe = SlowAfterCapPipe()
    published = threading.Event()
    seen = {}

    def on_captured(text, hit):
        seen["text"] = text
        seen["hit"] = hit
        published.set()

    def run():
        _drain_pipe(pipe, 40, on_captured=on_captured)

    t = threading.Thread(target=run)
    t.start()
    assert published.wait(timeout=2), "captured must publish before discard completes"
    assert pipe.second_started.wait(timeout=2)
    assert seen["hit"] is True
    assert len(seen["text"]) == 40
    assert seen["text"] == "A" * 40
    pipe.release_second.set()
    t.join(timeout=5)
    assert not t.is_alive()
