"""Пускатель процессов MCP, журнал запусков и уборка сирот (спека §5.12).

Процессы настоящие: тестовый stdio-сервер запускается через пускатель так
же, как его запустит транспорт, а сигналы уходят на самом деле. Каждое
ожидание — с потолком; всё порождённое добивают ``procs`` и ``mcp_reaper``.
"""

from __future__ import annotations

import json
import os
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest
from gc_core.tools.connect_mcp.process_journal import (
    ProcessJournal,
    journal_root,
    sweep_orphans,
)
from gc_core.utils.process_identity import (
    boot_id,
    parse_linux_stat,
    parse_ps_line,
    process_start_mark,
)
from gc_testkit.mcp import alive, children, launches, set_behavior, stdio_cfg
from gc_testkit.mcp import wait_for as _wait_for
from gc_testkit.mcp.fixtures import mcp_home, mcp_reaper

pytestmark = pytest.mark.skipif(
    os.name != "posix", reason="пускатель и сигналы групп — только POSIX"
)

_TIMEOUT = 5.0


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
    """Каталог журнала — третий аргумент пускателя."""
    _, argv = journal.launch_argv("true", [], "probe")
    return Path(argv[2])


# --- метка личности ---------------------------------------------------------


def test_parse_linux_stat_and_ps():
    tail = "1 4242 4242 0 -1 4194560 100 0 0 0 5 3 0 0 20 0 1 0 987654 1 2"
    assert parse_linux_stat(f"4242 (a b) c) S {tail}\n") == ("S", "987654")
    assert parse_linux_stat(f"4242 (sleep) Z {tail}") == ("Z", "987654")
    assert parse_ps_line("Ss   Wed Nov 12 10:00:00 2026\n") == (
        "Ss",
        "Wed Nov 12 10:00:00 2026",
    )
    assert parse_ps_line("Z+   Mon Nov  3 09:05:07 2026") == (
        "Z+",
        "Mon Nov 3 09:05:07 2026",
    )


def test_start_mark_of_live_zombie_and_missing(procs):
    own = process_start_mark(os.getpid())
    assert own is not None
    assert process_start_mark(os.getpid()) == own
    assert boot_id() not in ("", "0")
    assert boot_id() == boot_id()

    proc = subprocess.Popen([sys.executable, "-c", "pass"])
    procs.append(proc)
    deadline = time.monotonic() + _TIMEOUT
    while alive(proc.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    # Не пожат: процесс — зомби, метки нет.
    assert process_start_mark(proc.pid) is None
    proc.wait(_TIMEOUT)
    assert process_start_mark(proc.pid) is None


# --- пускатель и запись ------------------------------------------------------


def test_launch_argv_shape_and_owner(mcp_home):
    journal = ProcessJournal()
    cmd, args = journal.launch_argv("node", ["srv.js", "-x"], "my srv")
    jdir = Path(args[2])
    assert cmd == sys.executable
    assert args[0] == "-I"
    assert Path(args[1]).name == "spawn_launcher.py"
    assert args[3:] == ["my srv", "--", "node", "srv.js", "-x"]
    assert jdir.parent == journal_root() == mcp_home / "mcp" / "processes"
    owner = json.loads((jdir / "owner.json").read_text(encoding="utf-8"))
    assert owner["pid"] == os.getpid()
    assert owner["mark"] == process_start_mark(os.getpid())
    assert owner["boot_id"] == boot_id()
    assert owner["node"]
    parts = jdir.name.split("-")
    assert parts[1] == str(os.getpid())
    assert all(part.isalnum() for part in parts)
    # Тот же каталог на все запуски сервиса.
    assert journal.launch_argv("x", [], "k")[1][2] == str(jdir)


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
    assert launches(control) == [proc.pid]


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
        late.append(_spawn(procs, journal, tmp_path / "b.json"))

    # Запуск во время ожидания первого круга: в первом чтении его нет.
    timer = threading.Timer(0.1, launch_late)
    timer.start()
    journal.kill_all(wait_sec=2)
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
    done.stdin.close()
    done.wait(_TIMEOUT)
    # Пускатель не узнал метку, процесса нет: запись тоже снимается.
    unmarked = {"pid": 999999999, "pgid": 999999999, "mark": None}
    (_record_of(journal, live.pid).path.parent / "999999999.json").write_text(
        json.dumps({**unmarked, "server": "srv"}), encoding="utf-8"
    )
    journal.prune()
    assert [r.pid for r in journal.records()] == [live.pid]


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
    # В: журнал другой загрузки.
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
    for name in "ade":
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


def test_sweep_orphans_off_posix_does_nothing(mcp_home, monkeypatch):
    jdir = _journal_dir(ProcessJournal())
    _rewrite(jdir / "owner.json", boot_id="other-boot")
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
