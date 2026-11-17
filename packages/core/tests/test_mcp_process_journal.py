"""Пускатель процессов MCP, журнал запусков и уборка сирот (спека §5.12).

Процессы настоящие: тестовый stdio-сервер запускается через пускатель так
же, как его запустит транспорт, а сигналы уходят на самом деле. Каждое
ожидание — с потолком; всё порождённое добивают ``procs`` и ``mcp_reaper``.
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from gc_core.tools.connect_mcp import process_journal, spawn_launcher
from gc_core.tools.connect_mcp.process_journal import (
    ProcessJournal,
    journal_root,
    sweep_orphans,
)
from gc_core.utils import process_identity
from gc_core.utils.process_identity import (
    ProcessQueryFailed,
    boot_id,
    parse_linux_stat,
    parse_ps_table,
    process_start_mark,
    process_start_states,
)
from gc_testkit.mcp import alive, children, launches, set_behavior, stdio_cfg
from gc_testkit.mcp import wait_for as _wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_reaper

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="пускатель и сигналы групп — только POSIX"
)

_TIMEOUT = 5.0
_linux_only = pytest.mark.skipif(
    not sys.platform.startswith("linux"), reason="читает /proc"
)


async def wait_for(predicate) -> None:
    await _wait_for(predicate, timeout=_TIMEOUT)


@pytest.fixture
def procs():
    """Запущенные тестом процессы: в конце SIGKILL группе живого и wait.

    Сигнал — только непожатому процессу: пока ``poll()`` его не пожал,
    pid за ним, а группа (``start_new_session``) равна pid.
    """
    started: list[subprocess.Popen] = []
    yield started
    for proc in list(started):
        if proc.poll() is None:
            os.killpg(proc.pid, signal.SIGKILL)
        proc.wait(_TIMEOUT)
        if proc.stdin is not None:
            proc.stdin.close()


def _spawn(
    procs: list,
    journal: ProcessJournal,
    control: Path,
    *,
    key: str = "srv",
    extra_args: tuple[str, ...] = (),
    extra_env: dict[str, str] | None = None,
    **behavior,
) -> subprocess.Popen:
    set_behavior(control, **behavior)
    cfg = stdio_cfg(control)
    cmd, args = journal.launch_argv(
        cfg["command"], [*cfg["args"], *extra_args], key
    )
    proc = subprocess.Popen(
        [cmd, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        env={**os.environ, **cfg["env"], **(extra_env or {})},
        start_new_session=True,
    )
    procs.append(proc)
    return proc


async def _started(journal: ProcessJournal, control: Path, proc) -> None:
    """Пускатель записал файл, сервер после ``exec`` отметил запуск."""
    await wait_for(lambda: proc.pid in [r.pid for r in journal.records()])
    await wait_for(lambda: launches(control) == [proc.pid])


def _rewrite(path: Path, **fields) -> None:
    data = json.loads(path.read_text(encoding="utf-8"))
    data.update(fields)
    path.write_text(json.dumps(data), encoding="utf-8")


def _record_of(journal: ProcessJournal, pid: int):
    return next(r for r in journal.records() if r.pid == pid)


def _journal_dir(journal: ProcessJournal) -> Path:
    """Каталог журнала — аргумент пускателя после его пути."""
    _, argv = journal.launch_argv("true", [], "probe")
    return Path(argv[3])


def _fake_record(directory: Path, name: str, **record) -> None:
    payload = {"server": "srv", **record}
    (directory / f"{name}.json").write_text(json.dumps(payload), "utf-8")


async def _zombie_leader_with_deaf_child(procs, journal, control):
    """Лидер вышел сам и не пожат (зомби), глухой к SIGTERM потомок жив."""
    proc = _spawn(procs, journal, control, child_ignores_term=True)
    await _started(journal, control, proc)
    await wait_for(lambda: len(children(control)) == 1)
    [child] = children(control)
    proc.stdin.close()
    await wait_for(lambda: not alive(proc.pid))
    assert process_start_states([proc.pid])[proc.pid][1] is True
    assert alive(child)
    return proc, child


class _KillpgSpy:
    def __init__(self, error: BaseException | None = None) -> None:
        self.calls: list[tuple[int, int]] = []
        self._error = error

    def __call__(self, pgid: int, sig: int) -> None:
        self.calls.append((pgid, sig))
        if self._error is not None:
            raise self._error


# --- метка личности ---------------------------------------------------------


def test_parse_linux_stat_and_ps():
    tail = "1 4242 4242 0 -1 4194560 100 0 0 0 5 3 0 0 20 0 1 0 987654 1 2"
    assert parse_linux_stat(f"4242 (a b) c) S {tail}\n") == ("S", "987654")
    assert parse_linux_stat(f"4242 (sleep) Z {tail}") == ("Z", "987654")
    table = (
        "  101 Ss   Wed Nov 12 10:00:00 2026\n"
        "99999 Z+   Mon Nov  3 09:05:07 2026\n"
        "garbage\n"
    )
    assert parse_ps_table(table) == {
        101: ("Ss", "Wed Nov 12 10:00:00 2026"),
        99999: ("Z+", "Mon Nov 3 09:05:07 2026"),
    }
    assert parse_ps_table("") == {}


def test_start_mark_of_live_zombie_and_missing(procs):
    own = process_start_mark(os.getpid())
    assert own is not None
    assert process_start_mark(os.getpid()) == own
    assert boot_id() not in ("", "0")
    assert boot_id() == boot_id()

    reader = [sys.executable, "-c", "import sys; sys.stdin.read()"]
    proc = subprocess.Popen(reader, stdin=subprocess.PIPE)
    procs.append(proc)
    mark = process_start_mark(proc.pid)
    assert mark is not None
    assert process_start_states([proc.pid]) == {proc.pid: (mark, False)}
    proc.stdin.close()
    deadline = time.monotonic() + _TIMEOUT
    while alive(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    # Не пожат: зомби хранит время старта, но живой метки у него нет.
    assert process_start_mark(proc.pid) is None
    assert process_start_states([proc.pid]) == {proc.pid: (mark, True)}
    proc.wait(_TIMEOUT)
    assert process_start_mark(proc.pid) is None
    assert process_start_states([proc.pid, 0, -5]) == {}


def test_tools_are_found_by_absolute_path(monkeypatch, tmp_path):
    """Ветка macOS не ищет ``ps`` и ``sysctl`` по ``PATH``.

    Ветку проверяет procps ``/bin/ps`` Linux: формат ``-o lstart=`` тот же.
    """
    if not Path("/bin/ps").exists():
        pytest.skip("нет /bin/ps")
    fake = tmp_path / "bin"
    fake.mkdir()
    for name, line in (
        ("ps", f"{os.getpid()} S Fake Jan 1 00:00:00 1999"),
        ("sysctl", "fake-boot"),
    ):
        tool = fake / name
        tool.write_text(f"#!/bin/sh\necho '{line}'\n", encoding="utf-8")
        tool.chmod(0o755)
    real = subprocess.run(
        ["/bin/ps", "-o", "lstart=", "-p", str(os.getpid())],
        capture_output=True,
        text=True,
        env={**os.environ, "LC_ALL": "C"},
        check=True,
    ).stdout
    monkeypatch.setenv("PATH", str(fake))
    monkeypatch.setattr(sys, "platform", "darwin")
    mark = process_start_mark(os.getpid())
    assert mark == " ".join(real.split())
    assert spawn_launcher._start_mark() == mark
    assert boot_id() != "fake-boot"


def test_ps_rejecting_one_pid_does_not_hide_others(monkeypatch, tmp_path):
    """``ps`` macOS на pid выше своего предела отказывает целиком (код 1,
    пустой вывод): остальные pid опрашиваются по одному."""
    fake = tmp_path / "ps"
    fake.write_text(
        "#!/bin/sh\n"
        'case ",$4," in *,999999999,*)\n'
        '  echo "ps: process id too large: 999999999" >&2; exit 1;;\n'
        "esac\n"
        'for p in $(echo "$4" | tr , " "); do\n'
        '  [ "$p" = 7 ] || echo "$p S Fri Nov 13 12:11:31 2026"\n'
        "done\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    monkeypatch.setattr(process_identity, "_PS", str(fake))
    monkeypatch.setattr(sys, "platform", "darwin")

    states = process_start_states([5, 7, 999999999])

    assert states == {5: ("Fri Nov 13 12:11:31 2026", False)}


_REAL_RUN = process_identity._run
_QUERY_ERRORS = [
    subprocess.TimeoutExpired(["/bin/ps"], 5),
    OSError("ps не запустился"),
]


def _failing_ps(monkeypatch, error: BaseException, *, times: int = -1):
    """Ветка macOS: ``ps`` падает ``times`` раз (−1 — всегда), потом
    настоящий; ``sysctl`` отдаёт постоянную загрузку."""
    left = {"n": times}

    def run(argv, *, check):
        if argv[0] == process_identity._SYSCTL:
            return subprocess.CompletedProcess(argv, 0, "BOOT-1\n", "")
        if left["n"] != 0:
            left["n"] -= 1
            raise error
        return _REAL_RUN(argv, check=check)

    monkeypatch.setattr(sys, "platform", "darwin")
    monkeypatch.setattr(process_identity, "_run", run)


@pytest.mark.parametrize("error", _QUERY_ERRORS, ids=["timeout", "oserror"])
def test_failed_ps_is_unknown_not_missing(monkeypatch, error):
    """Опрос не удался — «неизвестно», а не «процесса нет»."""
    if not Path("/bin/ps").exists():
        pytest.skip("нет /bin/ps")
    _failing_ps(monkeypatch, error)
    with pytest.raises(ProcessQueryFailed):
        process_start_states([os.getpid()])
    with pytest.raises(ProcessQueryFailed):
        process_start_mark(os.getpid())


def _proc_stat_raises(monkeypatch, pid: int, error: OSError) -> None:
    """Чтение ``/proc/<pid>/stat`` этого pid падает с ``error``."""
    real_read = Path.read_text

    def read_text(self, *args, **kwargs):
        if str(self) == f"/proc/{pid}/stat":
            raise error
        return real_read(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", read_text)


@_linux_only
def test_unreadable_proc_is_unknown_not_missing(monkeypatch):
    _proc_stat_raises(monkeypatch, os.getpid(), OSError(5, "сбой чтения"))
    with pytest.raises(ProcessQueryFailed):
        process_start_states([os.getpid()])
    # Процесса нет — это ответ, а не сбой.
    assert process_start_states([999999999]) == {}


_FOREIGN_PID = 999999


@_linux_only
def test_foreign_proc_denied_is_missing(monkeypatch):
    # hidepid: чужой процесс (pid достался другому пользователю) не
    # читается. Свой читается всегда — отказ значит «не наш», а не сбой.
    denied = PermissionError(13, "нет доступа")
    _proc_stat_raises(monkeypatch, _FOREIGN_PID, denied)
    own = process_start_mark(os.getpid())
    states = process_start_states([os.getpid(), _FOREIGN_PID])
    assert states == {os.getpid(): (own, False)}


# --- пускатель и запись ------------------------------------------------------


def test_launch_argv_shape_and_owner(mcp_home):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("node", ["srv.js", "-x"], "my srv")
    jdir = Path(args[3])
    assert cmd == sys.executable
    assert args[:2] == ["-I", "-S"]
    assert Path(args[2]).name == "spawn_launcher.py"
    assert args[4:] == ["my srv", "--", "node", "srv.js", "-x"]
    assert jdir.parent == journal_root() == mcp_home / "mcp" / "processes"
    assert stat.S_IMODE(journal_root().stat().st_mode) == 0o700
    assert stat.S_IMODE(jdir.stat().st_mode) == 0o700
    assert stat.S_IMODE((jdir / "owner.json").stat().st_mode) == 0o600
    owner = json.loads((jdir / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()
    assert owner["mark"] == process_start_mark(os.getpid())
    assert owner["boot_id"] == boot_id()
    assert owner["node"]
    parts = jdir.name.split("-")
    assert parts[1] == str(os.getpid())
    assert all(part.isalnum() for part in parts)
    # Тот же каталог на все запуски сервиса.
    assert journal.launch_argv("x", [], "k")[1][3] == str(jdir)


def test_launch_argv_off_posix_is_plain(mcp_home, monkeypatch):
    monkeypatch.setattr(sys, "platform", "win32")
    journal = ProcessJournal()
    assert journal.launch_argv("node", ["a"], "srv") == ("node", ["a"])
    assert journal.records() == []
    assert not journal_root().exists()


async def test_launcher_keeps_pid_and_writes_record(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    [rec] = journal.records()
    assert rec.pid == proc.pid == rec.pgid
    assert rec.mark == process_start_mark(proc.pid)
    assert rec.mark is not None
    assert rec.server == "srv"
    assert rec.error is None
    assert stat.S_IMODE(rec.path.stat().st_mode) == 0o600
    assert launches(control) == [proc.pid]


async def test_launcher_is_transparent_on_stdio(mcp_home, procs):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("cat", [], "srv")
    proc = subprocess.Popen(
        [cmd, *args],
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    procs.append(proc)
    request = b'{"jsonrpc": "2.0", "id": 1, "method": "ping"}\n'
    out, err = proc.communicate(request, timeout=_TIMEOUT)
    assert (out, err, proc.returncode) == (request, b"", 0)
    assert [r.pid for r in journal.records()] == [proc.pid]


@_linux_only
def test_launcher_restores_default_signals(mcp_home):
    def ignored(argv: list[str]) -> str:
        status = subprocess.run(
            argv, capture_output=True, text=True, timeout=_TIMEOUT, check=True
        ).stdout
        return next(x for x in status.splitlines() if x.startswith("SigIgn"))

    cmd, args = ProcessJournal().launch_argv("cat", ["/proc/self/status"], "s")
    direct = ignored(["cat", "/proc/self/status"])
    assert ignored([cmd, *args]) == direct


async def test_each_launch_gets_own_record(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    first = _spawn(procs, journal, tmp_path / "a.json")
    second = _spawn(procs, journal, tmp_path / "b.json")
    await _started(journal, tmp_path / "a.json", first)
    await _started(journal, tmp_path / "b.json", second)
    assert sorted(r.pid for r in journal.records()) == sorted(
        [first.pid, second.pid]
    )


async def test_missing_command_recorded(mcp_home, procs):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("no-such-binary-xyz", [], "srv")
    proc = subprocess.Popen([cmd, *args], stderr=subprocess.DEVNULL)
    procs.append(proc)
    assert proc.wait(_TIMEOUT) == 127
    rec = journal.launch_error("srv")
    assert rec.error == "command_not_found"
    assert rec.detail
    assert "spawn_launcher" not in rec.detail
    assert journal.launch_error("other") is None


async def test_launch_error_is_only_the_newest_record(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("no-such-binary-xyz", [], "srv")
    failed = subprocess.Popen([cmd, *args], stderr=subprocess.DEVNULL)
    procs.append(failed)
    assert failed.wait(_TIMEOUT) == 127
    assert journal.launch_error("srv").pid == failed.pid
    # Удачный повторный запуск того же ключа гасит прежнюю причину.
    time.sleep(0.01)
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    assert journal.launch_error("srv") is None


async def test_prune_does_not_revive_superseded_launch_error(
    mcp_home, mcp_reaper, procs, tmp_path
):
    # Сбой, после которого ключ запускался заново, — уже не причина: когда
    # prune снимет запись вышедшего удачного запуска, сбой не всплывёт.
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("no-such-binary-xyz", [], "srv")
    failed = subprocess.Popen([cmd, *args], stderr=subprocess.DEVNULL)
    procs.append(failed)
    assert failed.wait(_TIMEOUT) == 127
    other = journal.launch_argv("no-such-binary-xyz", [], "other")
    lone = subprocess.Popen([other[0], *other[1]], stderr=subprocess.DEVNULL)
    procs.append(lone)
    assert lone.wait(_TIMEOUT) == 127
    time.sleep(0.01)
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    proc.stdin.close()
    proc.wait(_TIMEOUT)

    journal.prune()

    assert journal.launch_error("srv") is None
    assert [r.pid for r in journal.records()] == [lone.pid]


def test_host_identity_is_read_once(mcp_home, monkeypatch, tmp_path):
    # Метка и загрузка хоста — константы процесса: на macOS их чтение —
    # запуск утилит, на каждый журнал его не повторяем.
    reads: list[str] = []
    real_mark = process_journal.process_start_mark

    def mark(pid: int) -> str | None:
        reads.append(f"mark {pid}")
        return real_mark(pid)

    def boot() -> str:
        reads.append("boot")
        return "b1"

    monkeypatch.setattr(process_journal, "process_start_mark", mark)
    monkeypatch.setattr(process_journal, "boot_id", boot)
    monkeypatch.setattr(process_journal, "_HOST_IDENTITY", {})
    for n in range(3):
        ProcessJournal(tmp_path / f"j{n}").launch_argv("x", [], "k")
    assert reads == [f"mark {os.getpid()}", "boot"]


def test_failed_host_identity_is_not_remembered(mcp_home, monkeypatch):
    # Разовый сбой опроса при первом чтении не оставляет хозяина без
    # метки навсегда: следующий журнал читает метку заново.
    real_mark = process_journal.process_start_mark
    failures = {"left": 1}

    def mark(pid: int) -> str | None:
        if failures["left"]:
            failures["left"] -= 1
            raise ProcessQueryFailed("ps завис")
        return real_mark(pid)

    monkeypatch.setattr(process_journal, "process_start_mark", mark)
    monkeypatch.setattr(process_journal, "_HOST_IDENTITY", {})
    pid = os.getpid()
    assert process_journal._host_identity(pid)[0] is None
    assert process_journal._host_identity(pid)[0] == real_mark(pid)


def test_failed_journal_dir_is_not_retried(mcp_home, tmp_path, caplog):
    # Каталог не создать — запуски идут без учёта, а журнал не повторяет
    # попытку (и предупреждение) на каждом запуске.
    blocker = tmp_path / "file"
    blocker.write_text("")
    journal = ProcessJournal(blocker / "processes")
    with caplog.at_level("WARNING", logger=process_journal.__name__):
        argvs = [journal.launch_argv("srv", ["-x"], "k") for _ in range(3)]
    assert argvs == [("srv", ["-x"])] * 3
    warned = [r for r in caplog.records if "не создан" in r.getMessage()]
    assert len(warned) == 1


async def test_exec_failure_recorded(mcp_home, procs, tmp_path):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv(str(tmp_path), [], "srv")
    proc = subprocess.Popen([cmd, *args], stderr=subprocess.DEVNULL)
    procs.append(proc)
    assert proc.wait(_TIMEOUT) == 127
    rec = journal.launch_error("srv")
    assert rec.error == "exec_failed"
    assert rec.detail
    assert str(tmp_path) not in rec.detail


async def test_launcher_runs_server_without_journal(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    shutil.rmtree(_journal_dir(journal))
    proc = _spawn(procs, journal, control)
    await wait_for(lambda: launches(control) == [proc.pid])
    assert proc.poll() is None


async def test_no_secrets_in_journal(mcp_home, mcp_reaper, procs, tmp_path):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(
        procs,
        journal,
        control,
        extra_args=("--token=s3cret",),
        extra_env={"TOKEN": "s3cret"},
    )
    await _started(journal, control, proc)
    missing = journal.launch_argv("no-such-s3cret-xyz", ["s3cret"], "bad")
    failed = subprocess.Popen(
        [missing[0], *missing[1]],
        env={**os.environ, "TOKEN": "s3cret"},
        stderr=subprocess.DEVNULL,
    )
    procs.append(failed)
    assert failed.wait(_TIMEOUT) == 127
    files = list(mcp_home.rglob("*"))
    assert len([f for f in files if f.is_file()]) == 3
    for path in files:
        assert "s3cret" not in path.name
        if path.is_file():
            assert "s3cret" not in path.read_text(encoding="utf-8")


def test_records_skip_broken_files(mcp_home):
    journal = ProcessJournal()
    jdir = _journal_dir(journal)
    good = {"pid": 4242, "pgid": 4242, "mark": "1", "server": "srv"}
    (jdir / "4242.json").write_text(json.dumps(good), encoding="utf-8")
    (jdir / "4243.json").write_text('{"pid": 4243, "pg', encoding="utf-8")
    (jdir / "4244.json").write_text('["not", "a", "record"]', "utf-8")
    (jdir / "4245.json").write_text('{"pid": "x", "pgid": 1}', "utf-8")
    (jdir / ".4246.tmp").write_text(json.dumps(good), encoding="utf-8")
    (jdir / "junk.json").write_text(json.dumps(good), encoding="utf-8")
    [rec] = journal.records()
    assert (rec.pid, rec.pgid, rec.mark, rec.server) == (
        4242,
        4242,
        "1",
        "srv",
    )
    assert rec.path == jdir / "4242.json"


def test_foreign_owned_files_are_ignored(mcp_home, monkeypatch):
    journal = ProcessJournal()
    jdir = _journal_dir(journal)
    _fake_record(jdir, "4242", pid=4242, pgid=4242, mark="1")
    orphan_dir = _journal_dir(ProcessJournal())
    _rewrite(orphan_dir / "owner.json", mark="bogus", boot_id="other-boot")
    assert len(journal.records()) == 1
    real_uid = os.getuid()
    monkeypatch.setattr(os, "getuid", lambda: real_uid + 1)
    assert journal.records() == []
    assert sweep_orphans() == 0
    assert orphan_dir.exists()


# --- аварийное гашение -------------------------------------------------------


async def test_kill_all_kills_deaf_group(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control, ignore_term=True, spawn_child=True)
    await _started(journal, control, proc)
    await wait_for(lambda: len(children(control)) == 1)
    [child] = children(control)
    journal.kill_all(wait_sec=0.3)
    await wait_for(lambda: not alive(proc.pid) and not alive(child))


async def test_kill_all_kills_deaf_child_of_obedient_leader(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control, child_ignores_term=True)
    await _started(journal, control, proc)
    await wait_for(lambda: len(children(control)) == 1)
    [child] = children(control)
    started = time.monotonic()
    journal.kill_all(wait_sec=3)
    # Лидер вышел по SIGTERM — ожидание кончилось раньше срока.
    assert time.monotonic() - started < 2
    await wait_for(lambda: not alive(child))
    assert not alive(proc.pid)


async def test_kill_all_skips_foreign_mark_and_foreign_group(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    foreign = _spawn(procs, journal, tmp_path / "a.json")
    grouped = _spawn(procs, journal, tmp_path / "b.json")
    await _started(journal, tmp_path / "a.json", foreign)
    await _started(journal, tmp_path / "b.json", grouped)
    # pid достался другому процессу: метка не та.
    _rewrite(_record_of(journal, foreign.pid).path, mark="bogus")
    # Запущен не лидером группы: группа чужая. Номер группы — живой
    # процесс ``foreign``: неверное гашение его бы убило.
    _rewrite(_record_of(journal, grouped.pid).path, pgid=foreign.pid)
    journal.kill_all(wait_sec=0.2)
    time.sleep(0.2)
    assert alive(foreign.pid)
    assert alive(grouped.pid)


@_linux_only
async def test_kill_all_kills_own_despite_denied_foreign_pid(
    mcp_home, mcp_reaper, procs, tmp_path, monkeypatch
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    _fake_record(
        _journal_dir(journal),
        str(_FOREIGN_PID),
        pid=_FOREIGN_PID,
        pgid=_FOREIGN_PID,
        mark="1",
    )
    denied = PermissionError(13, "нет доступа")
    _proc_stat_raises(monkeypatch, _FOREIGN_PID, denied)
    journal.kill_all(wait_sec=0.3)
    await wait_for(lambda: not alive(proc.pid))


def _forged_records(directory: Path) -> None:
    """Записи с верными метками: своя группа хоста, init, 0, −5."""
    own = os.getpgrp()
    assert process_start_mark(own) is not None
    assert process_start_mark(1) is not None
    _fake_record(
        directory, "1", pid=own, pgid=own, mark=process_start_mark(own)
    )
    _fake_record(directory, "2", pid=1, pgid=1, mark=process_start_mark(1))
    _fake_record(directory, "3", pid=0, pgid=0, mark="0")
    _fake_record(directory, "4", pid=-5, pgid=-5, mark="0")


def test_kill_all_never_signals_own_group_or_low_pgid(mcp_home):
    journal = ProcessJournal()
    _forged_records(_journal_dir(journal))
    spy = _KillpgSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "killpg", spy)
        journal.kill_all(wait_sec=0.1)
    assert spy.calls == []


def test_sweep_never_signals_own_group_or_low_pgid(mcp_home):
    jdir = _journal_dir(ProcessJournal())
    _rewrite(jdir / "owner.json", mark="bogus")
    _forged_records(jdir)
    spy = _KillpgSpy()
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "killpg", spy)
        assert sweep_orphans(wait_sec=0.1) == 0
    assert spy.calls == []


async def test_kill_all_does_not_wait_for_refused_signal(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    spy = _KillpgSpy(PermissionError(1, "Operation not permitted"))
    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(os, "killpg", spy)
        started = time.monotonic()
        journal.kill_all(wait_sec=3)
        elapsed = time.monotonic() - started
    assert elapsed < 1
    assert spy.calls == [
        (proc.pid, signal.SIGTERM),
        (proc.pid, signal.SIGKILL),
    ]
    assert alive(proc.pid)


async def test_kill_all_kills_deaf_child_of_zombie_leader(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    proc, child = await _zombie_leader_with_deaf_child(
        procs, journal, tmp_path / "s.json"
    )
    journal.kill_all(wait_sec=1)
    await wait_for(lambda: not alive(child))


async def test_kill_all_returns_early_when_all_exit(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    control = tmp_path / "s.json"
    proc = _spawn(procs, journal, control)
    await _started(journal, control, proc)
    worker = threading.Thread(
        target=journal.kill_all, kwargs={"wait_sec": 10}, daemon=True
    )
    worker.start()
    worker.join(_TIMEOUT)
    # Вышедший лидер — зомби (не пожат тестом), он считается вышедшим.
    assert not worker.is_alive()
    assert not alive(proc.pid)


async def test_kill_all_rereads_journal_for_late_launch(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    deaf = _spawn(procs, journal, tmp_path / "a.json", ignore_term=True)
    await _started(journal, tmp_path / "a.json", deaf)
    late: list[subprocess.Popen] = []

    def launch_late() -> None:
        late.append(
            _spawn(procs, journal, tmp_path / "b.json", ignore_term=True)
        )

    # Запуск во время ожидания первого круга: в первом чтении его нет.
    timer = threading.Timer(0.1, launch_late)
    timer.start()
    started = time.monotonic()
    journal.kill_all(wait_sec=2)
    # Срок общий на оба круга: второй глухой не продлевает ожидание.
    assert time.monotonic() - started < 3
    timer.join(_TIMEOUT)
    [proc] = late
    await wait_for(lambda: not alive(proc.pid) and not alive(deaf.pid))
    assert proc.pid in [r.pid for r in journal.records()]


async def test_prune_drops_dead_records(mcp_home, mcp_reaper, procs, tmp_path):
    journal = ProcessJournal()
    done = _spawn(procs, journal, tmp_path / "a.json")
    live = _spawn(procs, journal, tmp_path / "b.json")
    await _started(journal, tmp_path / "a.json", done)
    await _started(journal, tmp_path / "b.json", live)
    zombie, _ = await _zombie_leader_with_deaf_child(
        procs, journal, tmp_path / "z.json"
    )
    cmd, args = journal.launch_argv("no-such-binary-xyz", [], "bad")
    failed = subprocess.Popen([cmd, *args], stderr=subprocess.DEVNULL)
    procs.append(failed)
    assert failed.wait(_TIMEOUT) == 127
    done.stdin.close()
    done.wait(_TIMEOUT)
    # Пускатель не узнал метку, процесса нет: запись тоже снимается.
    jdir = _record_of(journal, live.pid).path.parent
    _fake_record(jdir, "999999999", pid=999999999, pgid=999999999, mark=None)
    journal.prune()
    # Остаются живой, непожатый зомби-лидер и сбой запуска.
    assert sorted(r.pid for r in journal.records()) == sorted(
        [live.pid, zombie.pid, failed.pid]
    )


async def test_prune_and_kill_all_run_concurrently(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    started = [
        _spawn(procs, journal, tmp_path / f"{n}.json") for n in range(3)
    ]
    for n, proc in enumerate(started):
        await _started(journal, tmp_path / f"{n}.json", proc)
    errors: list[BaseException] = []
    stop = threading.Event()

    def prune_loop() -> None:
        try:
            while not stop.is_set():
                journal.prune()
        except BaseException as exc:  # noqa: BLE001 — собрать для assert
            errors.append(exc)

    worker = threading.Thread(target=prune_loop, daemon=True)
    worker.start()
    try:
        journal.kill_all(wait_sec=1)
    finally:
        stop.set()
        worker.join(_TIMEOUT)
    assert errors == []
    await wait_for(lambda: not any(alive(p.pid) for p in started))


@pytest.mark.parametrize("error", _QUERY_ERRORS, ids=["timeout", "oserror"])
def test_prune_keeps_records_when_query_fails(
    mcp_home, monkeypatch, procs, error
):
    if not Path("/bin/ps").exists():
        pytest.skip("нет /bin/ps")
    _failing_ps(monkeypatch, error, times=0)
    server = subprocess.Popen(["sleep", "300"], start_new_session=True)
    procs.append(server)
    journal = ProcessJournal()
    jdir = _journal_dir(journal)
    mark = process_start_mark(server.pid)
    _fake_record(
        jdir, str(server.pid), pid=server.pid, pgid=server.pid, mark=mark
    )
    _failing_ps(monkeypatch, error)
    journal.prune()
    journal.kill_all(wait_sec=0.2)
    assert [r.pid for r in journal.records()] == [server.pid]
    assert server.poll() is None
    # Опрос снова отвечает — запись на месте, kill_all гасит процесс.
    _failing_ps(monkeypatch, error, times=0)
    journal.kill_all(wait_sec=2)
    assert server.wait(_TIMEOUT) is not None


def test_await_exit_waits_full_term_when_query_fails(monkeypatch, tmp_path):
    def failing(pids):
        list(pids)
        raise ProcessQueryFailed("ps")

    monkeypatch.setattr(process_journal, "process_start_states", failing)
    record = process_journal.ProcessRecord(
        pid=4242,
        pgid=4242,
        mark="m",
        server="k",
        error=None,
        detail=None,
        path=tmp_path / "4242.json",
    )
    started = time.monotonic()
    process_journal._await_exit([record], started + 0.3)
    assert time.monotonic() - started >= 0.3


# --- сироты ------------------------------------------------------------------


async def test_sweep_orphans(mcp_home, mcp_reaper, procs, tmp_path):
    def case(name: str, **behavior):
        journal = ProcessJournal()
        control = tmp_path / f"{name}.json"
        proc = _spawn(procs, journal, control, **behavior)
        return journal, control, proc

    dead_owner = {"mark": "bogus"}
    # А: хозяин мёртв, сервер глух к закрытию stdin.
    ja, ca, pa = case("a", ignore_eof=True)
    # Б: хозяин — этот процесс.
    jb, cb, pb = case("b")
    # В: хозяин мёртв, журнал другой загрузки.
    jc, cc, pc = case("c")
    # Г: хозяин мёртв, лидер мёртв, потомок жив.
    jd, cd, pd = case("d", spawn_child=True)
    # Д: хозяин мёртв, метка записи не совпала.
    je, ce, pe = case("e")
    # Е: хозяин на другом узле.
    jf, cf, pf = case("f")
    for journal, control, proc in (
        (ja, ca, pa),
        (jb, cb, pb),
        (jc, cc, pc),
        (jd, cd, pd),
        (je, ce, pe),
        (jf, cf, pf),
    ):
        await _started(journal, control, proc)
    await wait_for(lambda: len(children(cd)) == 1)
    [orphan] = children(cd)
    pa.stdin.close()
    os.kill(pd.pid, signal.SIGKILL)
    pd.wait(_TIMEOUT)
    dirs = {
        name: _journal_dir(j)
        for name, j in zip("abcdef", (ja, jb, jc, jd, je, jf), strict=True)
    }
    for name in "acde":
        _rewrite(dirs[name] / "owner.json", **dead_owner)
    _rewrite(dirs["c"] / "owner.json", boot_id="other-boot")
    _rewrite(_record_of(je, pe.pid).path, mark="bogus")
    _rewrite(dirs["f"] / "owner.json", node="other-node", **dead_owner)

    assert sweep_orphans(wait_sec=2) == 1

    await wait_for(lambda: not alive(pa.pid))
    assert not dirs["a"].exists()
    assert dirs["b"].exists() and alive(pb.pid)
    assert not dirs["c"].exists() and alive(pc.pid)
    assert alive(orphan) and not dirs["d"].exists()
    assert alive(pe.pid) and not dirs["e"].exists()
    assert dirs["f"].exists() and alive(pf.pid)


async def test_sweep_orphans_kills_deaf_child_of_zombie_leader(
    mcp_home, mcp_reaper, procs, tmp_path
):
    journal = ProcessJournal()
    _, child = await _zombie_leader_with_deaf_child(
        procs, journal, tmp_path / "s.json"
    )
    jdir = _journal_dir(journal)
    _rewrite(jdir / "owner.json", mark="bogus")
    assert sweep_orphans(wait_sec=1) == 1
    await wait_for(lambda: not alive(child))
    assert not jdir.exists()


def test_sweep_orphans_owner_checks(mcp_home):
    live_other_boot = _journal_dir(ProcessJournal())
    _rewrite(live_other_boot / "owner.json", boot_id="other-boot")
    unmarked_live = _journal_dir(ProcessJournal())
    _rewrite(unmarked_live / "owner.json", mark=None)
    unmarked_dead = _journal_dir(ProcessJournal())
    _rewrite(unmarked_dead / "owner.json", pid=999999999, mark=None)
    assert sweep_orphans(wait_sec=0.1) == 0
    # Живой хозяин не трогается и при сбое boot_id (другая загрузка).
    assert live_other_boot.exists()
    assert unmarked_live.exists()
    assert not unmarked_dead.exists()


def test_sweep_orphans_off_posix_does_nothing(mcp_home, monkeypatch):
    jdir = _journal_dir(ProcessJournal())
    _rewrite(jdir / "owner.json", mark="bogus", boot_id="other-boot")
    monkeypatch.setattr(sys, "platform", "win32")
    assert sweep_orphans() == 0
    assert jdir.exists()


def test_remove_drops_the_directory(mcp_home):
    journal = ProcessJournal()
    jdir = _journal_dir(journal)
    journal.remove()
    assert not jdir.exists()
    assert journal.records() == []
    journal.remove()
    # Удалённый журнал каталога заново не заводит: команда — как есть.
    assert journal.launch_argv("srv", ["-x"], "k") == ("srv", ["-x"])
    assert list(journal_root().iterdir()) == []


@pytest.mark.parametrize("error", _QUERY_ERRORS, ids=["timeout", "oserror"])
@pytest.mark.parametrize("times", [1, -1], ids=["once", "always"])
def test_sweep_spares_live_neighbour_when_query_fails(
    mcp_home, monkeypatch, procs, error, times
):
    """Сбой опроса при проверке хозяина — каталог не трогать, сигналов нет."""
    if not Path("/bin/ps").exists():
        pytest.skip("нет /bin/ps")
    _failing_ps(monkeypatch, error, times=0)
    owner = subprocess.Popen(["sleep", "300"], start_new_session=True)
    server = subprocess.Popen(["sleep", "300"], start_new_session=True)
    procs.extend([owner, server])
    jdir = _journal_dir(ProcessJournal())
    _rewrite(
        jdir / "owner.json", pid=owner.pid, mark=process_start_mark(owner.pid)
    )
    _fake_record(
        jdir,
        str(server.pid),
        pid=server.pid,
        pgid=server.pid,
        mark=process_start_mark(server.pid),
    )
    _failing_ps(monkeypatch, error, times=times)

    assert sweep_orphans(wait_sec=0.3) == 0

    assert jdir.exists()
    assert owner.poll() is None and server.poll() is None


@pytest.mark.parametrize(
    ("platform_name", "boot", "ours"),
    [
        ("darwin", "BOOT-1", True),
        ("linux", "BOOT-1", False),
        ("darwin", "0", False),
    ],
    ids=["darwin-same-boot", "linux-same-boot", "darwin-unknown-boot"],
)
def test_renamed_node_is_same_host_only_on_darwin_by_boot(
    mcp_home, monkeypatch, platform_name, boot, ours
):
    """macOS меняет имя узла со сменой сети: свой журнал узнаётся по
    загрузке. На Linux контейнеры делят ядро и ``boot_id``, но не
    пространство pid — там только имя узла."""
    if not Path("/bin/ps").exists():
        pytest.skip("нет /bin/ps")
    jdir = _journal_dir(ProcessJournal())
    _rewrite(
        jdir / "owner.json",
        pid=999999999,
        mark=None,
        node="old-name",
        boot_id=boot,
    )
    monkeypatch.setattr(process_journal.platform, "node", lambda: "new-name")
    monkeypatch.setattr(process_journal, "boot_id", lambda: boot)
    monkeypatch.setattr(sys, "platform", platform_name)

    sweep_orphans(wait_sec=0.1)

    assert jdir.exists() is not ours
