import os
import stat as stat_module

import pytest

from tools.backends.local import LocalFsBackend
from tools.backends.protocol import ExecResult, FsBackend


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


def test_tool_backend_resolution(tmp_path):
    from types import SimpleNamespace
    from tools.fs.read_file import ReadFile
    # no ctx → LocalFsBackend
    t = ReadFile.__new__(ReadFile)
    t._ctx = None
    assert isinstance(t._backend, LocalFsBackend)
    # ctx.harbor_backend set → that object wins
    sentinel = object()
    t2 = ReadFile.__new__(ReadFile)
    t2._ctx = SimpleNamespace(harbor_backend=sentinel)
    assert t2._backend is sentinel
    # explicit override (tests) wins over everything
    t2._backend = "fake"
    assert t2._backend == "fake"
