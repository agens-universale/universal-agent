"""Тесты для swarm-эндпоинтов в src/server.py.

Покрывает:
- GET /swarm/status — lazy-check через lifecycle.check_and_finalize;
- 404 если sworm не существует;
- 503 если swarm.enabled=false;
- 400 на пустые system/task_id;
- финализация снимает sworm с monitor'а.

Полностью изолировано от хостового config.yml через мок-патчи на
`load_server_config` и `build_agent_config_for_server` ДО импорта server.
"""

import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

# Server-side зависимости (fastapi, starlette) ставятся через server_requirements.txt
# и могут отсутствовать в основном venv проекта. Если их нет — skip всех тестов.
pytest.importorskip("fastapi", reason="server-side dep, install via server_requirements.txt")
pytest.importorskip("starlette", reason="server-side dep, install via server_requirements.txt")

# Swarm runs the CLI inside a bubblewrap (bwrap) sandbox — Linux-only. Off Linux
# (e.g. macOS dev) these endpoint tests can't exercise real swarm behavior, so skip.
pytestmark = pytest.mark.skipif(
    sys.platform != "linux",
    reason="swarm is Linux-only (bubblewrap sandbox)",
)

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def _minimal_server_config():
    """Минимальный SERVER_CONFIG для тестов — без зависимости от хостового config.yml."""
    return {
        "server": {},
        "defaults": {"system": "generic"},
        "agent": {},
        "agent_overrides": {},
        "logging": {},
        "swarm": {},
        # Базовый threads_cleanup отключаем — он требует resolve_threads_dir
        # с валидным agent_config, чего у нас в тестах нет.
        "threads_cleanup": {"enabled": False},
    }


def _minimal_agent_config():
    """Минимальный agent_config_dict — пустые поля, server.py их использует только при /events/webhook."""
    return {}


@pytest.fixture
def make_server(tmp_path):
    """Создаёт изолированный экземпляр server с мокнутыми load_*_config + кастомным SWARM_CONFIG."""

    def factory(enabled=True):
        # Сбрасываем кеш server и зависимостей.
        for mod in list(sys.modules):
            if mod in ("server",) or mod.startswith("server."):
                del sys.modules[mod]

        threads_dir = tmp_path / "threads"
        sandbox_dir = tmp_path / "sandbox"
        threads_dir.mkdir(exist_ok=True)
        sandbox_dir.mkdir(exist_ok=True)

        # Патчим load_*_config до импорта server'а, чтобы не зависеть от config.yml хоста.
        with patch("rest_api.server_config.load_server_config", return_value=_minimal_server_config()), \
             patch("rest_api.server_config.build_agent_config_for_server", return_value=_minimal_agent_config()):
            import server as server_mod

        # Подменяем SWARM_CONFIG прямо в модуле.
        server_mod.SWARM_CONFIG = {
            "enabled": enabled,
            "threads_dir": str(threads_dir),
            "sandbox_root_dir": str(sandbox_dir),
            "max_concurrent": 10,
            "ttl_seconds": 86400,
            "limits": {},
            "cli_command": "gc-cli",
            "wrapper_path": "/usr/bin/true",
            "sandbox_tool": "bubblewrap",
            "agentmd_path": "",
            "skills_dir": "",
        }
        # Сбрасываем _swarm_monitor — fresh state на тест.
        server_mod._swarm_monitor = None

        return server_mod, str(threads_dir), str(sandbox_dir)

    return factory


def _seed_meta(threads_dir, sid, status, **extra):
    import swarm.storage as storage
    storage.save_swarm_meta(
        threads_dir, sid,
        base_meta={"system": "g", "task_id": sid.split("_", 1)[-1]},
        swarm_meta_patch={"id": sid, "status": status, **extra},
    )


def _write_completion(threads_dir, sid, payload):
    base = os.path.join(threads_dir, sid)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "completion.json"), "w") as f:
        json.dump(payload, f)


# ---------------------------------------------------------------------------
# Базовые ошибки
# ---------------------------------------------------------------------------


def test_status_503_when_swarm_disabled(make_server):
    server_mod, threads, sandbox = make_server(enabled=False)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.get("/swarm/status/g/t1")
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "swarm_disabled"


def test_status_404_when_meta_missing(make_server):
    server_mod, threads, sandbox = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.get("/swarm/status/g/no_such")
    assert resp.status_code == 404


def test_status_400_when_system_empty(make_server):
    server_mod, threads, sandbox = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    # %20 = space; после strip().lower() даст пустую строку → 400.
    resp = client.get("/swarm/status/%20/t1")
    assert resp.status_code == 400


# ---------------------------------------------------------------------------
# Lazy-check (шаг 1.5)
# ---------------------------------------------------------------------------


def test_status_returns_running_when_pid_alive(make_server, monkeypatch):
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t1", "running", pid=99999)

    # Мокаем os.kill как успешный (PID жив).
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/g/t1")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "running"
    assert body["system"] == "g"
    assert body["task_id"] == "t1"


def test_status_lazy_finalizes_via_completion_json(make_server):
    """Если есть completion.json — lazy-чек обновляет meta на completed."""
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t2", "running", pid=1)
    _write_completion(threads, "g_t2", {
        "exit_code": 0, "finished_at": "2026-06-04T12:00:00Z",
        "error_type": "", "error_message": "",
    })

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/g/t2")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"
    assert body["meta"]["swarm_meta"]["exit_code"] == 0
    assert body["meta"]["swarm_meta"]["sandbox_status"] == "completed"


def test_status_lazy_finalizes_dead_pid(make_server, monkeypatch):
    """PID мёртв + нет completion.json → status = unknown_failed."""
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t3", "running", pid=99999)
    # PID мёртв.
    def kill_dead(pid, sig):
        raise ProcessLookupError
    monkeypatch.setattr("os.kill", kill_dead)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/g/t3")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "unknown_failed"
    assert body["meta"]["swarm_meta"]["error_type"] == "process_crash"


def test_status_normalizes_system_lowercase(make_server, monkeypatch):
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "ci_build", "running", pid=99999)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/CI/build")
    assert resp.status_code == 200
    assert resp.json()["system"] == "ci"


def test_status_already_final_returns_as_is(make_server):
    """Финальный статус — никакой повторной финализации, возврат как есть."""
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t4", "completed", exit_code=0, pid=99999)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/g/t4")
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "completed"


def test_status_unregisters_from_monitor_when_finalized(make_server):
    """После lazy-финализации sworm должен быть снят с monitor.active_ids."""
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t5", "running", pid=1)
    _write_completion(threads, "g_t5", {
        "exit_code": 0, "finished_at": "x", "error_type": "", "error_message": "",
    })

    mock_monitor = MagicMock()
    server_mod._swarm_monitor = mock_monitor

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/status/g/t5")
    assert resp.status_code == 200
    assert resp.json()["status"] == "completed"

    mock_monitor.unregister_active.assert_called_once_with("g_t5")


# ===========================================================================
# POST /swarm/run — idempotency (M3, шаг 1.8)
# ===========================================================================


@pytest.fixture
def stub_runner(monkeypatch):
    """Мокает swarm_runner.start_swarm_process — возвращает (sid, pid) без реального Popen."""
    calls = []

    def fake_start(swarm_cfg, *, system, task_id, payload):
        sid = f"{system}_{task_id}"
        calls.append((sid, payload))
        # Симулируем, что start_swarm_process пишет meta (как реальный runner).
        import swarm.storage as storage
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"], sid,
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"id": sid, "status": "running", "pid": 99999},
        )
        return sid, 99999

    monkeypatch.setattr("swarm.runner.start_swarm_process", fake_start)
    return calls


def test_run_503_when_swarm_disabled(make_server):
    server_mod, _, _ = make_server(enabled=False)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json={"task_id": "x"})
    assert resp.status_code == 503
    assert resp.json()["detail"]["reason"] == "swarm_disabled"


def test_run_400_invalid_json(make_server):
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", data="not json")
    assert resp.status_code == 400


def test_run_400_body_not_object(make_server):
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json=["a", "b"])
    assert resp.status_code == 400


def test_run_starts_new_sworm_returns_started(make_server, stub_runner):
    server_mod, threads, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json={"task_id": "build-42"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["system"] == "generic"
    assert body["task_id"] == "build-42"
    assert len(stub_runner) == 1
    assert stub_runner[0][0] == "generic_build-42"


def test_run_409_when_swarm_id_active(make_server, stub_runner):
    """Повторный POST с тем же task_id когда sworm в running → 409."""
    server_mod, threads, _ = make_server(enabled=True)
    _seed_meta(threads, "generic_t1", "running", pid=99999)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.post("/swarm/run", json={"task_id": "t1"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["status"] == "rejected"
    assert body["reason"] == "swarm_id_already_active"
    assert body["current_status"] == "running"
    # start_swarm_process НЕ был вызван.
    assert stub_runner == []


def test_run_409_when_status_queued(make_server, stub_runner):
    server_mod, threads, _ = make_server(enabled=True)
    _seed_meta(threads, "generic_t2", "queued")

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.post("/swarm/run", json={"task_id": "t2"})
    assert resp.status_code == 409
    assert resp.json()["reason"] == "swarm_id_already_active"
    assert stub_runner == []


def test_run_409_when_swarm_id_finalized(make_server, stub_runner):
    """POST с task_id который уже завершился → 409 swarm_id_already_finalized (AR-3 default)."""
    server_mod, threads, _ = make_server(enabled=True)
    _seed_meta(threads, "generic_t3", "completed", exit_code=0)

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.post("/swarm/run", json={"task_id": "t3"})
    assert resp.status_code == 409
    body = resp.json()
    assert body["reason"] == "swarm_id_already_finalized"
    assert body["current_status"] == "completed"
    assert stub_runner == []


def test_run_501_when_reuse_strategy_continue(make_server, stub_runner):
    """AR-3 MVP: continue не поддерживается → 501."""
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json={"task_id": "x", "reuse_strategy": "continue"})
    assert resp.status_code == 501
    body = resp.json()
    assert body["status"] == "not_implemented"
    assert "reject" in body["supported_strategies"]


def test_run_501_when_reuse_strategy_fresh(make_server, stub_runner):
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json={"task_id": "x", "reuse_strategy": "fresh"})
    assert resp.status_code == 501


def test_run_503_when_max_concurrent_reached(make_server, stub_runner):
    server_mod, _, _ = make_server(enabled=True)
    server_mod.SWARM_CONFIG["max_concurrent"] = 2

    mock_monitor = MagicMock()
    mock_monitor.get_active_count.return_value = 2
    server_mod._swarm_monitor = mock_monitor

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.post("/swarm/run", json={"task_id": "newone"})
    assert resp.status_code == 503
    body = resp.json()
    assert body["reason"] == "too_many_concurrent_swarm_processes"
    assert stub_runner == []


def test_run_concurrent_posts_only_one_starts(make_server, stub_runner):
    """Два параллельных POST с одним task_id — ровно один запуск.

    Проверяем, что lock работает: внутри stub_runner симулируем нагрузку
    через time.sleep, в это время второй POST должен увидеть meta и
    получить 409.
    """
    import threading
    import time
    server_mod, threads, _ = make_server(enabled=True)

    started_count = [0]
    block_event = threading.Event()
    started_event = threading.Event()

    def fake_start(swarm_cfg, *, system, task_id, payload):
        sid = f"{system}_{task_id}"
        # Симулируем меdленный старт — записываем meta как running.
        import swarm.storage as storage
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"], sid,
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"id": sid, "status": "running", "pid": 99999},
        )
        started_count[0] += 1
        started_event.set()
        block_event.wait(timeout=2.0)  # держим lock
        return sid, 99999

    import swarm.runner as runner_mod
    runner_mod_orig = runner_mod.start_swarm_process
    runner_mod.start_swarm_process = fake_start

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    results = []

    def do_post():
        resp = client.post("/swarm/run", json={"task_id": "concurrent"})
        results.append((resp.status_code, resp.json()))

    try:
        t1 = threading.Thread(target=do_post)
        t1.start()
        # Дать первому потоку дойти до fake_start (и взять lock).
        started_event.wait(timeout=1.0)
        # Запустить второй — он должен ждать на lock'е, увидеть meta=running, получить 409.
        # Но lock в server у нас в начале → первый удерживает, второй тоже ждёт лока.
        # Чтобы тест отработал детерминированно — освобождаем первого после небольшой паузы.
        time.sleep(0.05)
        block_event.set()
        t1.join(timeout=2.0)

        # Теперь делаем второй POST — meta уже = running, должно быть 409.
        resp2 = client.post("/swarm/run", json={"task_id": "concurrent"})
        results.append((resp2.status_code, resp2.json()))
    finally:
        runner_mod.start_swarm_process = runner_mod_orig

    # Первый — started (200), второй — 409.
    assert started_count[0] == 1, f"start_swarm_process called {started_count[0]} times, expected 1"
    statuses = sorted(r[0] for r in results)
    assert statuses == [200, 409], statuses


def test_run_genesis_uuid_when_no_task_id(make_server, stub_runner):
    """Если task_id не передан — генерится новый UUID, каждый POST уникальный."""
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp1 = client.post("/swarm/run", json={})
    resp2 = client.post("/swarm/run", json={})
    assert resp1.status_code == 200
    assert resp2.status_code == 200
    assert resp1.json()["task_id"] != resp2.json()["task_id"]
    assert len(stub_runner) == 2  # оба started


def test_run_normalizes_system_lowercase(make_server, stub_runner):
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)

    resp = client.post("/swarm/run", json={"system": "CI", "task_id": "t"})
    assert resp.status_code == 200
    assert resp.json()["system"] == "ci"
    assert stub_runner[0][0] == "ci_t"


def test_run_registers_with_monitor(make_server, stub_runner):
    """После успешного start sworm должен быть зарегистрирован в monitor."""
    server_mod, _, _ = make_server(enabled=True)
    mock_monitor = MagicMock()
    mock_monitor.get_active_count.return_value = 0
    server_mod._swarm_monitor = mock_monitor

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.post("/swarm/run", json={"task_id": "x"})
    assert resp.status_code == 200
    mock_monitor.register_active.assert_called_once_with("generic_x")


# ===========================================================================
# GET /swarm/logs — channel E (M4/1.9)
# ===========================================================================


def test_logs_reads_messages_from_sandbox(make_server):
    """messages.jsonl лежит в sandbox-каталоге → endpoint должен его прочитать."""
    server_mod, threads, sandbox = make_server(enabled=True)
    _seed_meta(threads, "g_t", "completed", exit_code=0)

    # Создаём message в sandbox-side канале E.
    sandbox_msgs_dir = os.path.join(sandbox, "g_t", ".gc", "threads", "g_t")
    os.makedirs(sandbox_msgs_dir)
    with open(os.path.join(sandbox_msgs_dir, "messages.jsonl"), "w") as f:
        f.write('{"ts":"x","stream":"stdout","message":"from sandbox"}\n')

    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/logs/g/t")
    assert resp.status_code == 200
    body = resp.json()
    assert len(body["messages"]) == 1
    assert body["messages"][0]["message"] == "from sandbox"


def test_logs_404_when_no_data(make_server):
    server_mod, _, _ = make_server(enabled=True)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/logs/g/no_such")
    assert resp.status_code == 404


def test_logs_503_when_swarm_disabled(make_server):
    server_mod, _, _ = make_server(enabled=False)
    from starlette.testclient import TestClient
    client = TestClient(server_mod.app)
    resp = client.get("/swarm/logs/g/t")
    assert resp.status_code == 503


# ===========================================================================
# Graceful shutdown (M6 / шаг 3.3)
# ===========================================================================


def test_shutdown_kills_active_sworms(make_server, monkeypatch):
    """Shutdown handler: SIGTERM → grace → SIGKILL + meta = unknown_failed.

    Вызываем handler напрямую через asyncio, т.к. старый FastAPI on_event
    ненадёжно triggers через TestClient context.
    """
    import asyncio
    server_mod, threads, _ = make_server(enabled=True)
    _seed_meta(threads, "g_alive", "running", pid=12345)

    # Мок monitor с одним active sworm.
    mock_monitor = MagicMock()
    mock_monitor.get_active_ids_copy.return_value = {"g_alive"}
    server_mod._swarm_monitor = mock_monitor

    monkeypatch.setattr("os.kill", lambda pid, sig: None)  # все PID живые
    sigterm_calls = []
    sigkill_calls = []

    def fake_killpg(pid, sig):
        import signal as sig_mod
        if sig == sig_mod.SIGTERM:
            sigterm_calls.append(pid)
        elif sig == sig_mod.SIGKILL:
            sigkill_calls.append(pid)

    monkeypatch.setattr("os.killpg", fake_killpg)
    # Ускоряем grace period.
    async def fast_sleep(t):
        return
    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    # Вызываем shutdown handler напрямую.
    asyncio.run(server_mod._shutdown_swarm_processes())

    assert sigterm_calls == [12345]
    import swarm.storage as storage
    meta = storage.load_swarm_meta(threads, "g_alive")
    assert meta["swarm_meta"]["status"] == "unknown_failed"
    assert meta["swarm_meta"]["error_type"] == "server_shutdown"
    mock_monitor.unregister_active.assert_called_once_with("g_alive")
    mock_monitor.stop.assert_called_once()


def test_shutdown_handles_no_active_sworms(make_server):
    """Если active set пуст — shutdown ничего не делает, не падает."""
    import asyncio
    server_mod, _, _ = make_server(enabled=True)
    mock_monitor = MagicMock()
    mock_monitor.get_active_ids_copy.return_value = set()
    server_mod._swarm_monitor = mock_monitor

    # Не должно бросить.
    asyncio.run(server_mod._shutdown_swarm_processes())


def test_shutdown_noop_when_monitor_is_none(make_server):
    """Без monitor (swarm выключен) — shutdown handler выходит сразу."""
    import asyncio
    server_mod, _, _ = make_server(enabled=False)
    server_mod._swarm_monitor = None
    asyncio.run(server_mod._shutdown_swarm_processes())


def test_shutdown_sigkill_after_grace_if_still_alive(make_server, monkeypatch):
    """Если процесс жив после grace period — SIGKILL."""
    import asyncio
    server_mod, threads, _ = make_server(enabled=True)
    _seed_meta(threads, "g_stuck", "running", pid=12345)

    mock_monitor = MagicMock()
    mock_monitor.get_active_ids_copy.return_value = {"g_stuck"}
    server_mod._swarm_monitor = mock_monitor

    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    sigterm_calls, sigkill_calls = [], []

    def fake_killpg(pid, sig):
        import signal as sig_mod
        if sig == sig_mod.SIGTERM:
            sigterm_calls.append(pid)
        elif sig == sig_mod.SIGKILL:
            sigkill_calls.append(pid)

    monkeypatch.setattr("os.killpg", fake_killpg)
    async def fast_sleep(t):
        return
    monkeypatch.setattr("asyncio.sleep", fast_sleep)

    asyncio.run(server_mod._shutdown_swarm_processes())

    # Оба сигнала — SIGTERM сначала, потом SIGKILL (процесс мокается живым).
    assert sigterm_calls == [12345]
    assert sigkill_calls == [12345]
