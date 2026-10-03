import os
import sys
import threading

import pytest
from gc_core.backends.local import LocalFsBackend
from gc_core.backends.protocol import ExecResult, FsBackend
from gc_core.run_context import acreate_run_context
from gc_testkit.helpers.crossplatform import require_symlinks, shell_command
from gc_testkit.helpers.tool_ctx import CallableTool


def _py(code: str) -> str:
    """Команда «этот Python исполняет `code`» для оболочки ЭТОЙ ОС.

    Вместо `yes | head`, `sleep`, `$VAR` и `>&2`: этих слов нет в cmd.exe, а
    тесты проверяют бэкенд, а не синтаксис sh.
    """
    return shell_command([sys.executable, "-c", code])


@pytest.fixture()
def be():
    return LocalFsBackend()


def test_implements_protocol(be):
    assert isinstance(be, FsBackend)
    assert be.is_sandbox is False
    assert be.default_exec_timeout is None


async def test_stat_group(be, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"hello\n")  # байтами: write_text на Windows пишет \r\n
    p = str(f)
    assert await be.exists(p) and await be.lexists(p) and await be.is_file(p)
    assert not await be.is_dir(p) and await be.is_dir(str(tmp_path))
    assert await be.getsize(p) == 6
    info = await be.stat_info(p, follow_symlinks=False)
    assert info["size"] == 6 and info["is_file"] and not info["is_dir"]
    assert isinstance(info["mode"], int)
    assert await be.readable(p) and await be.writable(p)
    assert await be.realpath(p) == os.path.realpath(p)
    assert not await be.is_hidden(p)
    assert await be.is_hidden(str(tmp_path / ".hidden"))


async def test_read_group(be, tmp_path):
    f = tmp_path / "a.txt"
    f.write_bytes(b"l1\nl2\n")
    p = str(f)
    assert await be.read(p) == "l1\nl2\n"
    assert [line async for line in be.iter_lines(p)] == ["l1\n", "l2\n"]
    assert await be.read_head_bytes(p, 2) == b"l1"
    import hashlib

    assert await be.sha256_bytes(p) == hashlib.sha256(b"l1\nl2\n").hexdigest()


async def test_write_group_atomic_and_append(be, tmp_path):
    p = str(tmp_path / "w.txt")
    await be.write(p, "v1")
    assert await be.read(p) == "v1"
    assert not os.path.exists(p + ".tmp")  # atomic: no tmp residue
    await be.append(p, "-v2")
    assert await be.read(p) == "v1-v2"
    bp = str(tmp_path / "raw.bin")
    await be.write_bytes(bp, b"\x00\xff")
    with open(bp, "rb") as fd:
        assert fd.read() == b"\x00\xff"
    cp = str(tmp_path / "copy.bin")
    await be.copy(bp, cp)
    with open(cp, "rb") as fd:
        assert fd.read() == b"\x00\xff"
    nd = str(tmp_path / "n1" / "n2")
    await be.makedirs(nd)
    assert os.path.isdir(nd)


async def test_read_write_cp1251_round_trip(be, tmp_path):
    p = str(tmp_path / "legacy.txt")
    text = (
        "Уважаемые коллеги! Настоящим сообщаем, что заседание рабочей группы "
        "по внедрению системы состоится 15 марта 2024 года в переговорной.\n"
    ) * 3
    await be.write(p, text, encoding="cp1251")
    assert open(p, "rb").read() == text.encode("cp1251")
    assert await be.read(p) == text


async def test_write_unsupported_encoding_raises(be, tmp_path):
    p = str(tmp_path / "x.txt")
    with pytest.raises(ValueError, match="Unsupported encoding"):
        await be.write(p, "hi", encoding="ibm866")


async def test_write_encode_failed_raises(be, tmp_path):
    p = str(tmp_path / "x.txt")
    with pytest.raises(ValueError, match="Cannot encode"):
        await be.write(p, "Привет", encoding="latin-1")


async def test_traverse_and_paths(be, tmp_path):
    (tmp_path / "sub").mkdir()
    (tmp_path / "sub" / "f.txt").write_text("x", encoding="utf-8")
    (tmp_path / "top.md").write_text("y", encoding="utf-8")
    root = str(tmp_path)
    assert await be.listdir(root) == [("sub", True), ("top.md", False)]
    assert sorted(await be.walk(root)) == [
        ("sub", True),
        ("sub/f.txt", False),
        ("top.md", False),
    ]
    assert be.normpath(root + "/") == os.path.normpath(root + "/")
    assert be.join(root, "x") == os.path.join(root, "x")


async def test_exec_ok_and_timeout(be, tmp_path):
    res = await be.aexec("echo hi", cwd=str(tmp_path), timeout=10)
    assert isinstance(res, ExecResult)
    assert (
        res.return_code == 0
        and res.stdout.strip() == "hi"
        and not res.timed_out
    )
    res = await be.aexec(_py("import time; time.sleep(5)"), timeout=1)
    assert res.timed_out and res.return_code is None
    env_res = await be.aexec(
        _py("import os; print(os.environ['GC_T'])"),
        timeout=10,
        env={"GC_T": "42"},
    )
    assert env_res.stdout.strip() == "42"


async def test_run_python(be, tmp_path):
    res = await be.arun_python("print('ok')", cwd=str(tmp_path), timeout=10)
    assert res.return_code == 0 and res.stdout.strip() == "ok"
    res = await be.arun_python("import time; time.sleep(5)", timeout=1)
    assert res.timed_out


def test_default_cwd(be):
    assert be.default_cwd() == os.getcwd()


async def test_tool_backend_resolution():
    from types import SimpleNamespace

    from gc_core.tools.fs.read_file import ReadFile

    from gc_testkit.settings import make_settings

    cfg = make_settings(
        {"backend_api_mode": "chat_completions", "save_logs": False}
    )
    ctx = await acreate_run_context(cfg)
    t = CallableTool(ReadFile(ctx=ctx))
    assert isinstance(t._backend, LocalFsBackend)
    assert t._backend is ctx.os_backend

    sentinel = object()
    t2 = ReadFile.__new__(ReadFile)
    t2._ctx = SimpleNamespace(os_backend=sentinel)
    assert t2._backend is sentinel


async def test_walk_symlink_to_dir_is_file_entry(be, tmp_path):
    """Symlink to a directory must be is_dir=False in walk (lstat parity with listdir)."""
    require_symlinks(tmp_path)
    sub = tmp_path / "sub"
    sub.mkdir()
    (sub / "inner.txt").write_text("x", encoding="utf-8")
    link = tmp_path / "linkdir"
    link.symlink_to(sub)
    root = str(tmp_path)
    walked = dict(await be.walk(root))
    assert walked.get("linkdir") is False
    assert walked.get("sub") is True
    assert "sub/inner.txt" in walked


async def test_walk_symlink_to_file(be, tmp_path):
    require_symlinks(tmp_path)
    real = tmp_path / "real.txt"
    real.write_text("x", encoding="utf-8")
    link = tmp_path / "link.txt"
    link.symlink_to(real)
    walked = dict(await be.walk(str(tmp_path)))
    assert walked.get("link.txt") is False


async def test_write_replace_failure_cleans_tmp(be, tmp_path, monkeypatch):
    p = str(tmp_path / "target.txt")
    (tmp_path / "target.txt").write_text("keep", encoding="utf-8")
    calls = {"n": 0}

    def flaky_replace(src, dst):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated replace failure")

    monkeypatch.setattr(os, "replace", flaky_replace)
    with pytest.raises(OSError, match="simulated"):
        await be.write(p, "new content")
    assert not os.path.exists(p + ".tmp")
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "keep"


async def test_write_bytes_replace_failure_cleans_tmp(
    be, tmp_path, monkeypatch
):
    p = str(tmp_path / "bin.dat")
    monkeypatch.setattr(
        os, "replace", lambda _s, _d: (_ for _ in ()).throw(OSError("nope"))
    )
    with pytest.raises(OSError):
        await be.write_bytes(p, b"\xff\xfe")
    assert not os.path.exists(p + ".tmp")
    assert not os.path.exists(p)


async def test_read_head_bytes_binary(be, tmp_path):
    p = str(tmp_path / "raw.bin")
    data = b"hello\x00world\xff"
    with open(p, "wb") as fd:
        fd.write(data)
    assert await be.read_head_bytes(p, 8) == data[:8]


# --- bounded exec runner (spec D1/D2/D9/D10) ---


async def test_exec_result_has_capture_cap_flags(be):
    res = await be.aexec("echo hi", timeout=10)
    assert isinstance(res, ExecResult)
    assert res.stdout_hit_capture_cap is False
    assert res.stderr_hit_capture_cap is False


def test_exec_default_capture_cap_is_40k(be):
    assert be.exec_output_capture_max_chars == 40_000


async def test_exec_spawns_no_helper_threads(be):
    """Ни одного вспомогательного потока на вызов.

    Прежняя реализация держала два дренажных потока: pipe нельзя не читать —
    заполненный буфер блокирует запись в дочернем процессе. Теперь ту же
    работу делают две корутины, и тест сторожит, чтобы потоки не вернулись
    (раньше здесь проверялось, что они хотя бы демонские).
    """
    before = threading.active_count()
    res = await be.aexec("echo hi", timeout=10)
    assert res.return_code == 0
    assert threading.active_count() == before


async def test_exec_publishes_bounded_capture_without_waiting_for_full_drain(
    be,
):
    """Захват ограничен, но процесс дочитывается и завершается сам.

    Раньше это проверялось на приватном _drain_pipe; теперь — на поведении
    exec, то есть на том, что реально важно вызывающему.
    """
    be.exec_output_capture_max_chars = 40
    # sys.executable, а не голое `python`: имени `python` в PATH может не быть
    # (в окружениях uv/pyenv ставится только `python3`), и тест падал бы на
    # `command not found` — не на том свойстве, которое проверяет.
    res = await be.aexec(
        f"{sys.executable} -c \"print('z' * 20000)\"", timeout=15
    )

    assert len(res.stdout) <= 40
    assert res.stdout_hit_capture_cap is True
    assert res.return_code == 0
    assert res.timed_out is False


async def test_exec_per_call_capture_max_overrides_default(be):
    be.exec_output_capture_max_chars = 50
    res = await be.aexec(
        _py("import sys; sys.stdout.write('A' * 5000)"),
        timeout=15,
        capture_max_chars=400,
    )
    assert res.return_code == 0
    assert len(res.stdout) <= 400
    assert len(res.stdout) > 50
    assert res.stdout_hit_capture_cap is True


async def test_exec_bounded_stdout_not_killed(be):
    """Output exceeding cap is bounded; process is NOT killed (drain & discard)."""
    be.exec_output_capture_max_chars = 200
    # Print ~5000 chars to stdout; process must finish on its own (exit 0).
    res = await be.aexec(
        _py("import sys; sys.stdout.write('A' * 5000)"), timeout=15
    )
    assert res.return_code == 0
    assert not res.timed_out
    assert len(res.stdout) <= 200
    assert res.stdout_hit_capture_cap is True
    assert res.stderr_hit_capture_cap is False


async def test_exec_bounded_stderr(be):
    be.exec_output_capture_max_chars = 150
    # Redirect to stderr only.
    res = await be.aexec(
        _py("import sys; sys.stderr.write('B' * 5000)"), timeout=15
    )
    assert res.return_code == 0
    assert len(res.stderr) <= 150
    assert res.stderr_hit_capture_cap is True
    assert res.stdout_hit_capture_cap is False


async def test_exec_both_pipes_large_no_deadlock(be):
    """Both stdout and stderr large simultaneously must not deadlock the runner."""
    be.exec_output_capture_max_chars = 100
    cmd = _py(
        "import sys; sys.stdout.write('O' * 8000); sys.stderr.write('E' * 8000)"
    )
    res = await be.aexec(cmd, timeout=20)
    assert res.return_code == 0
    assert len(res.stdout) <= 100
    assert len(res.stderr) <= 100
    assert res.stdout_hit_capture_cap is True
    assert res.stderr_hit_capture_cap is True


async def test_exec_small_output_no_cap_hit(be):
    be.exec_output_capture_max_chars = 60_000
    res = await be.aexec("echo small", timeout=10)
    assert res.return_code == 0
    assert res.stdout.strip() == "small"
    assert res.stdout_hit_capture_cap is False
    assert res.stderr_hit_capture_cap is False


async def test_exec_timeout_partial_buffer(be):
    """On timeout: timed_out=True, exit_code=None, partial buffer preserved."""
    be.exec_output_capture_max_chars = 60_000
    res = await be.aexec(
        _py("import time; print('started', flush=True); time.sleep(5)"),
        timeout=1,
    )
    assert res.timed_out is True
    assert res.return_code is None
    assert "started" in res.stdout


async def test_run_python_bounded_output(be):
    be.exec_output_capture_max_chars = 120
    code = "print('A' * 5000)"
    res = await be.arun_python(code, timeout=15)
    assert res.return_code == 0
    assert len(res.stdout) <= 120
    assert res.stdout_hit_capture_cap is True


async def test_run_python_per_call_capture_max_overrides_default(be):
    be.exec_output_capture_max_chars = 50
    res = await be.arun_python(
        "print('A' * 5000)", timeout=15, capture_max_chars=400
    )
    assert res.return_code == 0
    assert len(res.stdout) <= 400
    assert len(res.stdout) > 50
    assert res.stdout_hit_capture_cap is True


async def test_run_python_small_output_no_cap(be):
    be.exec_output_capture_max_chars = 60_000
    res = await be.arun_python("print('py-ok')", timeout=10)
    assert res.return_code == 0
    assert res.stdout.strip() == "py-ok"
    assert res.stdout_hit_capture_cap is False


async def test_run_python_bounded_stderr(be):
    be.exec_output_capture_max_chars = 100
    code = "import sys; sys.stderr.write('E' * 4000)"
    res = await be.arun_python(code, timeout=15)
    assert res.return_code == 0
    assert len(res.stderr) <= 100
    assert res.stderr_hit_capture_cap is True


async def test_run_python_timeout(be, tmp_path):
    res = await be.arun_python(
        "import time; print('go'); time.sleep(5)", cwd=str(tmp_path), timeout=1
    )
    assert res.timed_out is True
    assert res.return_code is None
    assert "go" in res.stdout


async def test_drain_keeps_reading_after_head_is_full():
    """После заполнения головы чтение продолжается: счётчик и хвост настоящие.

    Свойство пришло с develop, где чтение синхронное и живёт в потоке. У нас
    это корутина над трубой (`_adrain_stream`): важно, что чтение НЕ
    прекращается по заполнении головы — иначе ни счётчик, ни хвост не будут
    настоящими, а вызывающий получит только начало вывода.
    """
    from gc_core.backends.local import _adrain_stream, _Capture

    class _Stream:
        """Двойник `StreamReader`: отдаёт заготовленные чанки, потом EOF."""

        def __init__(self, chunks) -> None:
            self._chunks = list(chunks)

        async def read(self, _n):
            return self._chunks.pop(0) if self._chunks else b""

    sink = _Capture(40)
    await _adrain_stream(_Stream([b"A" * 50, b"B" * 50, b"END"]), sink)
    cs = sink.stream

    assert cs.total_chars == 103, (
        "счётчик считает только голову — чтение обрывается"
    )
    assert cs.truncated is True
    assert cs.head == "A" * 20
    assert cs.tail.endswith("END"), f"хвост не настоящий: {cs.tail!r}"
    assert len(cs.tail) == 20
