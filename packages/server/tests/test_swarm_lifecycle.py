"""Тесты для src/swarm/lifecycle.py.

Покрывает:
- _is_pid_alive (с моком os.kill)
- _determine_status_from_completion (детерминированная функция)
- check_and_finalize (7 сценариев финализации)

Все вызовы os.kill — мокаем, чтобы тесты были детерминированными
и не зависели от реальных PID'ов в системе.
"""

import json
import os

import pytest
from gc_server.swarm import lifecycle as lc
from gc_server.swarm import storage as storage

# ---------------------------------------------------------------------------
# _is_pid_alive
# ---------------------------------------------------------------------------


def test_is_pid_alive_zero_returns_false():
    assert lc._is_pid_alive(0) is False


def test_is_pid_alive_negative_returns_false():
    assert lc._is_pid_alive(-1) is False


def test_is_pid_alive_existing_pid_returns_true(monkeypatch):
    # os.kill(pid, 0) ничего не делает → процесс жив.
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)
    assert lc._is_pid_alive(12345) is True


def test_is_pid_alive_lookup_error_returns_false(monkeypatch):
    def raise_lookup(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", raise_lookup)
    assert lc._is_pid_alive(12345) is False


def test_is_pid_alive_permission_error_returns_true(monkeypatch):
    """Чужой процесс — не наш, но он точно жив."""

    def raise_perm(pid, sig):
        raise PermissionError

    monkeypatch.setattr(os, "kill", raise_perm)
    assert lc._is_pid_alive(1) is True


# ---------------------------------------------------------------------------
# _determine_status_from_completion
# ---------------------------------------------------------------------------


def test_determine_status_exit_zero_no_error_completed():
    r = lc._determine_status_from_completion(
        {"exit_code": 0, "error_type": ""}
    )
    assert r == "completed"


def test_determine_status_exit_nonzero_no_error_failed():
    r = lc._determine_status_from_completion(
        {"exit_code": 1, "error_type": ""}
    )
    assert r == "failed"


def test_determine_status_error_type_timeout():
    r = lc._determine_status_from_completion(
        {"exit_code": 124, "error_type": "timeout"}
    )
    assert r == "timeout"


def test_determine_status_error_type_cli_error_failed():
    r = lc._determine_status_from_completion(
        {"exit_code": 1, "error_type": "cli_error"}
    )
    assert r == "failed"


def test_determine_status_error_type_signal_term_failed():
    r = lc._determine_status_from_completion(
        {"exit_code": 143, "error_type": "signal_term"}
    )
    assert r == "failed"


def test_determine_status_error_type_disk_limit_failed():
    r = lc._determine_status_from_completion(
        {"exit_code": 137, "error_type": "disk_limit"}
    )
    assert r == "failed"


def test_determine_status_missing_exit_code_failed():
    r = lc._determine_status_from_completion({"error_type": ""})
    assert r == "failed"


def test_determine_status_non_int_exit_code_failed():
    r = lc._determine_status_from_completion(
        {"exit_code": "0", "error_type": ""}
    )
    assert r == "failed"


# ---------------------------------------------------------------------------
# FINAL_STATUSES
# ---------------------------------------------------------------------------


def test_final_statuses_contains_expected():
    # cancelled — отмена задачи через API /v1 (POST …/cancel).
    assert (
        frozenset(
            {"completed", "failed", "timeout", "unknown_failed", "cancelled"}
        )
        == lc.FINAL_STATUSES
    )


# ---------------------------------------------------------------------------
# check_and_finalize — 7 сценариев
# ---------------------------------------------------------------------------


@pytest.fixture
def dirs(tmp_path):
    threads = tmp_path / "threads"
    sandbox = tmp_path / "sandbox"
    threads.mkdir()
    sandbox.mkdir()
    return str(threads), str(sandbox)


def _seed_meta(threads_dir: str, sid: str, status: str, **extra):
    """Helper для создания meta.json с заданным статусом."""
    storage.save_swarm_meta(
        threads_dir,
        sid,
        base_meta={"system": "g", "task_id": sid.split("_", 1)[-1]},
        swarm_meta_patch={"id": sid, "status": status, **extra},
    )


def _write_completion(threads_dir: str, sid: str, payload: dict):
    base = os.path.join(threads_dir, sid)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "completion.json"), "w") as f:
        json.dump(payload, f)


def test_no_meta_returns_empty(dirs):
    threads, sandbox = dirs
    assert lc.check_and_finalize(threads, sandbox, "nope") == {}


def test_already_final_unchanged(dirs):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t1", "completed", exit_code=0, pid=99999)
    before = storage.load_swarm_meta(threads, "g_t1")["updated_at"]
    r = lc.check_and_finalize(threads, sandbox, "g_t1")
    assert r["swarm_meta"]["status"] == "completed"
    # updated_at не должен поменяться (мы не делали save)
    assert r["updated_at"] == before


def test_completion_exit_zero_becomes_completed(dirs):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t2", "running", pid=1)
    _write_completion(
        threads,
        "g_t2",
        {
            "exit_code": 0,
            "finished_at": "2026-06-04T11:00:00Z",
            "error_type": "",
            "error_message": "",
        },
    )
    r = lc.check_and_finalize(threads, sandbox, "g_t2")
    sm = r["swarm_meta"]
    assert sm["status"] == "completed"
    assert sm["exit_code"] == 0
    assert sm["finished_at"] == "2026-06-04T11:00:00Z"
    assert sm["sandbox_status"] == "completed"


def test_completion_exit_nonzero_becomes_failed(dirs):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t3", "running", pid=1)
    _write_completion(
        threads,
        "g_t3",
        {
            "exit_code": 1,
            "finished_at": "x",
            "error_type": "cli_error",
            "error_message": "boom",
        },
    )
    r = lc.check_and_finalize(threads, sandbox, "g_t3")
    sm = r["swarm_meta"]
    assert sm["status"] == "failed"
    assert sm["error_message"] == "boom"
    assert sm["sandbox_status"] == "completed"


def test_completion_timeout_becomes_timeout(dirs):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t4", "running", pid=1)
    _write_completion(
        threads,
        "g_t4",
        {
            "exit_code": 124,
            "finished_at": "x",
            "error_type": "timeout",
            "error_message": "killed",
        },
    )
    r = lc.check_and_finalize(threads, sandbox, "g_t4")
    assert r["swarm_meta"]["status"] == "timeout"


def test_dead_pid_no_completion_becomes_unknown_failed(dirs, monkeypatch):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t5", "running", pid=99999)

    # Мокаем os.kill чтобы он бросал ProcessLookupError (PID мёртв).
    def kill_dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr(os, "kill", kill_dead)

    r = lc.check_and_finalize(threads, sandbox, "g_t5")
    sm = r["swarm_meta"]
    assert sm["status"] == "unknown_failed"
    assert sm["error_type"] == "process_crash"
    assert "finished_at" in sm
    assert sm["sandbox_status"] == "completed"


def test_alive_pid_no_completion_unchanged(dirs, monkeypatch):
    threads, sandbox = dirs
    _seed_meta(threads, "g_t6", "running", pid=99999)
    # Мокаем: процесс жив.
    monkeypatch.setattr(os, "kill", lambda pid, sig: None)

    r = lc.check_and_finalize(threads, sandbox, "g_t6")
    assert r["swarm_meta"]["status"] == "running"


def test_pid_zero_no_completion_becomes_unknown_failed(dirs):
    """Если PID не задан или нулевой, тоже считаем мёртвым."""
    threads, sandbox = dirs
    _seed_meta(threads, "g_t7", "running")  # без pid
    r = lc.check_and_finalize(threads, sandbox, "g_t7")
    assert r["swarm_meta"]["status"] == "unknown_failed"


def test_check_and_finalize_idempotent_on_already_final(dirs):
    """Повторный вызов для финального sworm'а — никаких изменений."""
    threads, sandbox = dirs
    _seed_meta(threads, "g_t8", "running", pid=1)
    _write_completion(
        threads,
        "g_t8",
        {
            "exit_code": 0,
            "finished_at": "x",
            "error_type": "",
            "error_message": "",
        },
    )
    r1 = lc.check_and_finalize(threads, sandbox, "g_t8")
    updated_at_1 = r1["updated_at"]
    r2 = lc.check_and_finalize(threads, sandbox, "g_t8")
    updated_at_2 = r2["updated_at"]
    # Второй вызов попадает в early return — meta не пересохраняется.
    assert updated_at_1 == updated_at_2
    assert r2["swarm_meta"]["status"] == "completed"


# ---------------------------------------------------------------------------
# Шаг 2.1 — Timeout enforcement через _kill_process_tree
# ---------------------------------------------------------------------------


def test_parse_iso_deadline_valid():
    d = lc._parse_iso_deadline("2026-06-04T10:00:00+00:00")
    assert d is not None
    assert d.year == 2026


def test_parse_iso_deadline_naive_treated_as_utc():
    d = lc._parse_iso_deadline("2026-06-04T10:00:00")
    assert d is not None
    assert d.tzinfo is not None  # awaresched


def test_parse_iso_deadline_invalid_returns_none():
    assert lc._parse_iso_deadline("not a date") is None
    assert lc._parse_iso_deadline("") is None
    assert lc._parse_iso_deadline(None) is None
    assert lc._parse_iso_deadline(12345) is None


def test_kill_process_tree_nonexistent_pid_does_not_raise(monkeypatch):
    """Если pid не существует — silent return."""

    def raise_lookup(*args, **kwargs):
        raise ProcessLookupError

    monkeypatch.setattr("os.killpg", raise_lookup)
    # Не должно бросить.
    lc._kill_process_tree(99999)


def test_kill_process_tree_zero_pid_noop():
    lc._kill_process_tree(0)  # просто return
    lc._kill_process_tree(-1)


def test_kill_process_tree_sigterm_then_sigkill(monkeypatch):
    """SIGTERM послан → grace period → SIGKILL если жив."""
    import signal as sig_mod

    sent_signals = []

    def fake_killpg(pid, sigval):
        sent_signals.append(sigval)

    monkeypatch.setattr("os.killpg", fake_killpg)
    # PID всегда жив → дойдёт до SIGKILL.
    monkeypatch.setattr(lc, "_is_pid_alive", lambda pid: True)
    # Сокращаем grace period для теста.
    lc._kill_process_tree(12345, grace_period=0.05)

    assert sent_signals[0] == sig_mod.SIGTERM
    assert sent_signals[-1] == sig_mod.SIGKILL


def test_kill_process_tree_no_sigkill_if_died_during_grace(monkeypatch):
    import signal as sig_mod

    sent_signals = []

    def fake_killpg(pid, sigval):
        sent_signals.append(sigval)

    monkeypatch.setattr("os.killpg", fake_killpg)
    # Вся группа мертва сразу после SIGTERM.
    monkeypatch.setattr(lc, "_is_group_alive", lambda pid: False)
    lc._kill_process_tree(12345, grace_period=0.05)

    assert sent_signals == [sig_mod.SIGTERM]  # без SIGKILL


def test_check_and_finalize_timeout_kills_process(dirs, monkeypatch):
    """deadline_at прошёл + PID жив → killpg + status=timeout."""
    threads, sandbox = dirs
    past = "2020-01-01T00:00:00+00:00"  # уже прошёл
    _seed_meta(threads, "g_to", "running", pid=12345, deadline_at=past)

    # Мокаем killpg + alive check (PID живой).
    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)  # alive
    # Сократить grace.
    monkeypatch.setattr(lc, "_KILL_GRACE_SECONDS", 0.01)

    r = lc.check_and_finalize(threads, sandbox, "g_to")
    sm = r["swarm_meta"]
    assert sm["status"] == "timeout"
    assert sm["error_type"] == "timeout"
    assert "Killed by Python-side timeout" in sm["error_message"]
    assert sm["sandbox_status"] == "completed"
    assert killed, "ожидали хотя бы один os.killpg вызов"


def test_check_and_finalize_no_timeout_when_deadline_in_future(
    dirs, monkeypatch
):
    """deadline_at в будущем → процесс не убиваем."""
    threads, sandbox = dirs
    future = "2099-01-01T00:00:00+00:00"
    _seed_meta(threads, "g_tf", "running", pid=12345, deadline_at=future)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )

    r = lc.check_and_finalize(threads, sandbox, "g_tf")
    assert r["swarm_meta"]["status"] == "running"
    assert killed == []


def test_check_and_finalize_deadline_passed_but_process_already_dead(
    dirs, monkeypatch
):
    """deadline_at прошёл, но процесс уже сам завершился → fallthrough.

    Если есть completion.json — финализируется по нему.
    Если нет — unknown_failed (через PID-чек ниже).
    """
    threads, sandbox = dirs
    past = "2020-01-01T00:00:00+00:00"
    _seed_meta(threads, "g_tdead", "running", pid=99999, deadline_at=past)
    _write_completion(
        threads,
        "g_tdead",
        {
            "exit_code": 0,
            "finished_at": "x",
            "error_type": "",
            "error_message": "",
        },
    )

    # PID мёртв.
    def kill_dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr("os.kill", kill_dead)
    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )

    r = lc.check_and_finalize(threads, sandbox, "g_tdead")
    # Прошли через completion.json ветку, не через timeout.
    assert r["swarm_meta"]["status"] == "completed"
    assert killed == []


def test_check_and_finalize_invalid_deadline_skips_timeout_logic(
    dirs, monkeypatch
):
    """Невалидный deadline_at — игнорируем timeout-чек, идём к completion/PID."""
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_tinv", "running", pid=99999, deadline_at="not a date"
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    r = lc.check_and_finalize(threads, sandbox, "g_tinv")
    # Падаем в обычный PID-чек, PID жив → без изменений.
    assert r["swarm_meta"]["status"] == "running"


# ---------------------------------------------------------------------------
# Шаг 2.2 — Disk quota monitoring
# ---------------------------------------------------------------------------


def test_get_dir_size_mb_returns_none_for_missing_dir():
    assert lc._get_dir_size_mb("/no/such/dir") is None


def test_get_dir_size_mb_returns_zero_for_empty_dir(tmp_path):
    d = tmp_path / "empty"
    d.mkdir()
    size_mb = lc._get_dir_size_mb(str(d))
    # du -sb считает блок каталога (~4 KB); os.walk fallback — 0.
    assert 0.0 <= size_mb < 0.01


def test_get_dir_size_mb_counts_files(tmp_path):
    d = tmp_path / "f"
    d.mkdir()
    # 2 MB файл.
    (d / "blob.bin").write_bytes(b"x" * (2 * 1024 * 1024))
    size_mb = lc._get_dir_size_mb(str(d))
    assert 1.9 < size_mb < 2.1


def test_disk_limit_kills_process_when_exceeded(dirs, monkeypatch):
    """heavy_checks=True + sandbox_path > disk_mb → kill + failed."""
    threads, sandbox = dirs
    # Создаём sandbox path с большим файлом.
    sb_path = os.path.join(sandbox, "g_d1")
    os.makedirs(sb_path)
    (
        open(os.path.join(sb_path, "big.bin"), "wb").write(
            b"x" * (3 * 1024 * 1024)
        )
    )
    _seed_meta(
        threads,
        "g_d1",
        "running",
        pid=12345,
        sandbox_path=sb_path,
        limits={"disk_mb": 1},  # 1MB limit, файл 3MB → violation
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(lc, "_KILL_GRACE_SECONDS", 0.01)

    r = lc.check_and_finalize(threads, sandbox, "g_d1", heavy_checks=True)
    sm = r["swarm_meta"]
    assert sm["status"] == "failed"
    assert sm["error_type"] == "disk_limit"
    assert "exceeds" in sm["error_message"]
    assert sm["sandbox_status"] == "completed"
    assert killed


def test_disk_limit_not_enforced_without_heavy_checks(dirs, monkeypatch):
    """Без heavy_checks — disk не проверяется (lazy mode для /status endpoint)."""
    threads, sandbox = dirs
    sb_path = os.path.join(sandbox, "g_d2")
    os.makedirs(sb_path)
    open(os.path.join(sb_path, "big.bin"), "wb").write(
        b"x" * (3 * 1024 * 1024)
    )
    _seed_meta(
        threads,
        "g_d2",
        "running",
        pid=12345,
        sandbox_path=sb_path,
        limits={"disk_mb": 1},
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    # heavy_checks=False по умолчанию.
    r = lc.check_and_finalize(threads, sandbox, "g_d2")
    assert r["swarm_meta"]["status"] == "running"


def test_disk_limit_skipped_when_below_threshold(dirs, monkeypatch):
    threads, sandbox = dirs
    sb_path = os.path.join(sandbox, "g_d3")
    os.makedirs(sb_path)
    open(os.path.join(sb_path, "small.bin"), "wb").write(
        b"x" * 100
    )  # 100 байт
    _seed_meta(
        threads,
        "g_d3",
        "running",
        pid=12345,
        sandbox_path=sb_path,
        limits={"disk_mb": 100},
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    r = lc.check_and_finalize(threads, sandbox, "g_d3", heavy_checks=True)
    assert r["swarm_meta"]["status"] == "running"


def test_disk_limit_skipped_when_no_sandbox_path(dirs, monkeypatch):
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_d4", "running", pid=12345, limits={"disk_mb": 1}
    )  # без sandbox_path
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    r = lc.check_and_finalize(threads, sandbox, "g_d4", heavy_checks=True)
    assert r["swarm_meta"]["status"] == "running"


# ---------------------------------------------------------------------------
# Шаг 2.3 — Memory monitoring через psutil
# ---------------------------------------------------------------------------


def test_memory_limit_kills_when_psutil_reports_excess(dirs, monkeypatch):
    """psutil показывает RSS > memory_mb → kill + failed."""
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_m1", "running", pid=12345, limits={"memory_mb": 100}
    )  # 100 MB
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(lc, "_KILL_GRACE_SECONDS", 0.01)

    # Мок psutil.Process.memory_info().rss = 200 MB.
    if not lc._PSUTIL_AVAILABLE:
        pytest.skip("psutil не установлен")

    class FakeMemInfo:
        rss = 200 * 1024 * 1024  # 200 MB

    class FakeProcess:
        def __init__(self, pid):
            pass

        def memory_info(self):
            return FakeMemInfo()

    monkeypatch.setattr(lc.psutil, "Process", FakeProcess)

    r = lc.check_and_finalize(threads, sandbox, "g_m1", heavy_checks=True)
    sm = r["swarm_meta"]
    assert sm["status"] == "failed"
    assert sm["error_type"] == "memory_limit"
    assert "200" in sm["error_message"]


def test_memory_limit_skipped_without_psutil(dirs, monkeypatch):
    """Без psutil — memory check просто пропускается, нет ошибки."""
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_m2", "running", pid=12345, limits={"memory_mb": 1}
    )  # entry: 1 MB very low
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr(lc, "_PSUTIL_AVAILABLE", False)

    r = lc.check_and_finalize(threads, sandbox, "g_m2", heavy_checks=True)
    assert r["swarm_meta"]["status"] == "running"


def test_memory_limit_handles_psutil_no_such_process(dirs, monkeypatch):
    """psutil.NoSuchProcess → не падаем, идём дальше."""
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_m3", "running", pid=12345, limits={"memory_mb": 100}
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    if not lc._PSUTIL_AVAILABLE:
        pytest.skip("psutil не установлен")

    def raise_nope(pid):
        raise lc.psutil.NoSuchProcess(pid)

    monkeypatch.setattr(lc.psutil, "Process", raise_nope)

    r = lc.check_and_finalize(threads, sandbox, "g_m3", heavy_checks=True)
    assert r["swarm_meta"]["status"] == "running"


# ---------------------------------------------------------------------------
# Шаг 2.4 — CPU monitoring через psutil
# ---------------------------------------------------------------------------


def test_cpu_limit_kills_when_psutil_reports_excess(dirs, monkeypatch):
    threads, sandbox = dirs
    _seed_meta(
        threads, "g_c1", "running", pid=12345, limits={"cpu_percent": 50}
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    killed = []
    monkeypatch.setattr(
        "os.killpg", lambda pid, sig: killed.append((pid, sig))
    )
    monkeypatch.setattr(lc, "_KILL_GRACE_SECONDS", 0.01)

    if not lc._PSUTIL_AVAILABLE:
        pytest.skip("psutil не установлен")

    class FakeMemInfo:
        rss = 1  # tiny

    class FakeProcess:
        def __init__(self, pid):
            pass

        def memory_info(self):
            return FakeMemInfo()

        def cpu_percent(self, interval=None):
            return 95.0  # 95%

    monkeypatch.setattr(lc.psutil, "Process", FakeProcess)

    r = lc.check_and_finalize(threads, sandbox, "g_c1", heavy_checks=True)
    sm = r["swarm_meta"]
    assert sm["status"] == "failed"
    assert sm["error_type"] == "cpu_limit"
    assert "95" in sm["error_message"]


def test_resource_limits_not_checked_when_heavy_false(dirs, monkeypatch):
    """heavy_checks=False (default из endpoint'а) → пропускаем все resource checks."""
    threads, sandbox = dirs
    sb_path = os.path.join(sandbox, "g_h1")
    os.makedirs(sb_path)
    open(os.path.join(sb_path, "big.bin"), "wb").write(
        b"x" * (5 * 1024 * 1024)
    )
    _seed_meta(
        threads,
        "g_h1",
        "running",
        pid=12345,
        sandbox_path=sb_path,
        limits={"disk_mb": 1, "memory_mb": 1, "cpu_percent": 1},
    )
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    r = lc.check_and_finalize(
        threads, sandbox, "g_h1"
    )  # heavy_checks=False default
    assert r["swarm_meta"]["status"] == "running"


# ---------------------------------------------------------------------------
# _persist_agent_artifacts: сохранение agent logs/threads из песочницы на хост
# ---------------------------------------------------------------------------


def _make_fake_sandbox(tmp_path, swarm_id):
    """Создать sandbox/<id>/.gc/{logs,threads} с файлами-маркерами."""
    sb = tmp_path / "sandboxes" / swarm_id
    (sb / ".gc" / "logs").mkdir(parents=True)
    (sb / ".gc" / "threads" / swarm_id).mkdir(parents=True)
    (sb / ".gc" / "logs" / "agent_main.log").write_text("LOGMARK")
    (sb / ".gc" / "threads" / swarm_id / "messages.jsonl").write_text(
        "MSGMARK"
    )
    return sb


def test_persist_agent_artifacts_default_under_threads_dir(tmp_path):
    """Без настроенного swarm_logs_dir — копирует .gc/logs в threads_dir/<id>/agent_logs."""
    from gc_server.swarm.lifecycle import _persist_agent_artifacts

    swarm_id = "generic_t1"
    sb = _make_fake_sandbox(tmp_path, swarm_id)
    threads_dir = str(tmp_path / "threads")
    _persist_agent_artifacts(threads_dir, swarm_id, str(sb))
    assert (
        tmp_path / "threads" / swarm_id / "agent_logs" / "agent_main.log"
    ).read_text() == "LOGMARK"


def test_persist_agent_artifacts_configured_dir(tmp_path):
    """С настроенным swarm_logs_dir — копирует в <swarm_logs_dir>/<id>/."""
    from gc_server.swarm.lifecycle import _persist_agent_artifacts

    swarm_id = "generic_t2"
    sb = _make_fake_sandbox(tmp_path, swarm_id)
    threads_dir = str(tmp_path / "threads")
    logs_dir = str(tmp_path / "custom_logs")
    _persist_agent_artifacts(
        threads_dir, swarm_id, str(sb), swarm_logs_dir=logs_dir
    )
    assert (
        tmp_path / "custom_logs" / swarm_id / "agent_main.log"
    ).read_text() == "LOGMARK"
    # В дефолтный threads_dir НЕ должно попасть.
    assert not (tmp_path / "threads" / swarm_id / "agent_logs").exists()


def test_persist_agent_artifacts_missing_sandbox_noop(tmp_path):
    """Пустой sandbox_path — no-op, без ошибок."""
    from gc_server.swarm.lifecycle import _persist_agent_artifacts

    _persist_agent_artifacts(str(tmp_path / "threads"), "x", "")  # не падает
