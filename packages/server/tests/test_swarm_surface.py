"""Поверхность swarm: ручки вебхука, задачи — процессы песочниц.

Запуск песочницы и сигналы подменены: bubblewrap здесь не запускается.
Нужен POSIX API ``os.killpg``, поэтому файл идёт и на macOS.
"""

import asyncio
import json
import os
import threading
import time
from unittest.mock import MagicMock

import pytest
from fastapi.testclient import TestClient
from gc_server.swarm import storage
from server_harness import make_swarm, make_webhook

pytestmark = pytest.mark.skipif(
    os.name != "posix",
    reason="shutdown tests require POSIX process-group API",
)


@pytest.fixture
def swarm(tmp_path):
    threads = tmp_path / "threads"
    sandbox = tmp_path / "sandbox"
    threads.mkdir()
    sandbox.mkdir()
    surface = make_swarm(
        {
            "enabled": True,
            "threads_dir": str(threads),
            "sandbox_root_dir": str(sandbox),
            "max_concurrent": 10,
            "ttl_seconds": 86400,
            "limits": {},
            "cli_command": "gc-cli",
            "wrapper_path": "/usr/bin/true",
            "sandbox_tool": "bubblewrap",
            "agentmd_path": "",
            "skills_dir": "",
        },
        tmp_path,
    )
    surface.paths = (str(threads), str(sandbox))
    return surface


def _client(surface) -> TestClient:
    return TestClient(surface.app())


def _sid(system: str, task_id: str) -> str:
    return f"swarm_{system}_{task_id}"


def _seed(threads, sid, status, *, system="g", task_id=None, **extra):
    if task_id is None:
        rest = sid.removeprefix("swarm_")
        system, task_id = rest.split("_", 1)
    storage.save_swarm_meta(
        threads,
        sid,
        base_meta={"system": system, "task_id": task_id},
        swarm_meta_patch={"id": sid, "status": status, **extra},
    )


def _completion(threads, sid, payload):
    base = os.path.join(threads, sid)
    os.makedirs(base, exist_ok=True)
    with open(os.path.join(base, "completion.json"), "w") as f:
        json.dump(payload, f)


@pytest.fixture
def stub_runner(monkeypatch):
    """Запуск песочницы без Popen: пишет meta, как настоящий runner."""
    calls = []

    def fake_start(swarm_cfg, *, system, task_id, payload, trace_id=None):
        sid = _sid(system, task_id)
        calls.append((sid, payload, trace_id))
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"],
            sid,
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"id": sid, "status": "running", "pid": 99999},
        )
        return sid, 99999

    monkeypatch.setattr(
        "gc_server.swarm.runner.start_swarm_process", fake_start
    )
    return calls


# --- статус -----------------------------------------------------------------


def test_status_404_when_meta_missing(swarm):
    assert _client(swarm).get("/runs/g/no_such").status_code == 404


def test_status_400_when_system_empty(swarm):
    assert _client(swarm).get("/runs/%20/t1").status_code == 400


def test_status_running_when_pid_alive(swarm, monkeypatch):
    threads, _ = swarm.paths
    _seed(threads, _sid("g", "t1"), "running", pid=99999)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    body = _client(swarm).get("/runs/g/t1").json()
    assert body["status"] == "running"
    assert (body["system"], body["task_id"]) == ("g", "t1")
    assert body["swarm"]["status"] == "running"


def test_status_finalizes_via_completion_json(swarm):
    threads, _ = swarm.paths
    sid = _sid("g", "t2")
    _seed(threads, sid, "running", pid=1)
    _completion(
        threads,
        sid,
        {
            "exit_code": 0,
            "finished_at": "2026-06-04T12:00:00Z",
            "error_type": "",
            "error_message": "",
        },
    )
    body = _client(swarm).get("/runs/g/t2").json()
    assert body["status"] == "completed"
    assert body["finished_at"] == "2026-06-04T12:00:00Z"
    assert body["swarm"]["exit_code"] == 0
    assert body["swarm"]["sandbox_status"] == "completed"


def test_status_dead_pid_is_failed(swarm, monkeypatch):
    threads, _ = swarm.paths
    _seed(threads, _sid("g", "t3"), "running", pid=99999)

    def kill_dead(pid, sig):
        raise ProcessLookupError

    monkeypatch.setattr("os.kill", kill_dead)
    body = _client(swarm).get("/runs/g/t3").json()
    assert body["status"] == "failed"
    assert body["swarm"]["status"] == "unknown_failed"
    assert body["swarm"]["error_type"] == "process_crash"


def test_status_normalizes_system_lowercase(swarm, monkeypatch):
    threads, _ = swarm.paths
    _seed(threads, _sid("ci", "build"), "running", pid=99999)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    assert _client(swarm).get("/runs/CI/build").json()["system"] == "ci"


def test_status_unregisters_from_monitor_when_finalized(swarm):
    threads, _ = swarm.paths
    sid = _sid("g", "t5")
    _seed(threads, sid, "running", pid=1)
    _completion(
        threads,
        sid,
        {
            "exit_code": 0,
            "finished_at": "x",
            "error_type": "",
            "error_message": "",
        },
    )
    swarm.monitor = MagicMock()
    assert _client(swarm).get("/runs/g/t5").json()["status"] == "completed"
    swarm.monitor.unregister_active.assert_called_once_with(sid)


# --- старт задачи -----------------------------------------------------------


def test_start_400_invalid_json(swarm):
    resp = _client(swarm).post("/events/webhook", content="not json")
    assert resp.status_code == 400


def test_start_400_body_not_object(swarm):
    resp = _client(swarm).post("/events/webhook", json=["a", "b"])
    assert resp.status_code == 400


def test_start_returns_started(swarm, stub_runner):
    resp = _client(swarm).post("/events/webhook", json={"task_id": "build-42"})
    assert resp.status_code == 200
    body = resp.json()
    assert body["status"] == "started"
    assert body["task_id"] == "build-42"
    assert stub_runner[0][0] == _sid("generic", "build-42")


def test_start_forwards_trace_id_and_strips_it_from_payload(
    swarm, stub_runner
):
    resp = _client(swarm).post(
        "/events/webhook", json={"task_id": "t-tr", "trace_id": "abc-1"}
    )
    assert resp.status_code == 200
    _sid_, payload, trace_id = stub_runner[0]
    assert trace_id == "abc-1"
    assert "trace_id" not in payload


def test_start_400_on_invalid_trace_id(swarm, stub_runner):
    resp = _client(swarm).post(
        "/events/webhook", json={"task_id": "t-bad", "trace_id": "wf\n42"}
    )
    assert resp.status_code == 400
    assert "invalid trace_id" in resp.json()["detail"]["payload"]
    assert stub_runner == []  # процесс не стартовал


@pytest.mark.parametrize("status", ["running", "queued"])
def test_start_409_when_task_is_running(swarm, stub_runner, status):
    threads, _ = swarm.paths
    _seed(threads, _sid("generic", "t1"), status, pid=99999)
    resp = _client(swarm).post("/events/webhook", json={"task_id": "t1"})
    assert resp.status_code == 409
    detail = resp.json()["detail"]
    assert detail["status"] == "error"
    assert detail["payload"] == "task is running"
    assert detail["current_status"] == status
    assert stub_runner == []


def test_start_409_when_task_finished(swarm, stub_runner):
    threads, _ = swarm.paths
    _seed(threads, _sid("generic", "t3"), "completed", exit_code=0)
    resp = _client(swarm).post("/events/webhook", json={"task_id": "t3"})
    assert resp.status_code == 409
    assert "new task_id" in resp.json()["detail"]["payload"]


def test_start_503_when_max_concurrent_reached(swarm, stub_runner):
    swarm.monitor = MagicMock()
    swarm.monitor.get_active_count.return_value = 10
    resp = _client(swarm).post("/events/webhook", json={"task_id": "newone"})
    assert resp.status_code == 503
    detail = resp.json()["detail"]
    assert detail["status"] == "error"
    assert "too many concurrent" in detail["payload"]
    assert stub_runner == []


def test_reserved_system_is_400(swarm, stub_runner):
    resp = _client(swarm).post(
        "/events/webhook", json={"task_id": "t", "system": "a2a"}
    )
    assert resp.status_code == 400
    assert stub_runner == []


def test_concurrent_posts_start_only_once(swarm, monkeypatch):
    """Два одновременных запроса с одним task_id — один запуск, второй —
    409 по meta, записанной первым под замком."""
    started = [0]
    block, entered = threading.Event(), threading.Event()

    def fake_start(swarm_cfg, *, system, task_id, payload, trace_id=None):
        sid = _sid(system, task_id)
        storage.save_swarm_meta(
            swarm_cfg["threads_dir"],
            sid,
            base_meta={"system": system, "task_id": task_id},
            swarm_meta_patch={"id": sid, "status": "running", "pid": 99999},
        )
        started[0] += 1
        entered.set()
        block.wait(timeout=2.0)
        return sid, 99999

    monkeypatch.setattr(
        "gc_server.swarm.runner.start_swarm_process", fake_start
    )
    client = _client(swarm)
    results = []

    def post():
        resp = client.post("/events/webhook", json={"task_id": "concurrent"})
        results.append(resp.status_code)

    first = threading.Thread(target=post)
    first.start()
    entered.wait(timeout=1.0)
    time.sleep(0.05)
    block.set()
    first.join(timeout=2.0)
    post()
    assert started[0] == 1
    assert sorted(results) == [200, 409]


def test_new_task_id_when_none_sent(swarm, stub_runner):
    client = _client(swarm)
    one = client.post("/events/webhook", json={}).json()
    two = client.post("/events/webhook", json={}).json()
    assert one["task_id"] != two["task_id"]
    assert len(stub_runner) == 2


def test_start_normalizes_system_lowercase(swarm, stub_runner):
    resp = _client(swarm).post(
        "/events/webhook", json={"system": "CI", "task_id": "t"}
    )
    assert resp.json()["system"] == "ci"
    assert stub_runner[0][0] == _sid("ci", "t")


def test_start_registers_with_monitor(swarm, stub_runner):
    swarm.monitor = MagicMock()
    swarm.monitor.get_active_count.return_value = 0
    _client(swarm).post("/events/webhook", json={"task_id": "x"})
    swarm.monitor.register_active.assert_called_once_with(_sid("generic", "x"))


# --- логи -------------------------------------------------------------------


def test_logs_read_messages_from_sandbox(swarm):
    threads, sandbox = swarm.paths
    sid = _sid("g", "t")
    _seed(threads, sid, "completed", exit_code=0)
    work = os.path.join(sandbox, sid, ".gc", "threads")
    os.makedirs(work, exist_ok=True)
    with open(os.path.join(sandbox, sid, "messages.jsonl"), "w") as f:
        f.write(json.dumps({"role": "assistant", "content": "ok"}) + "\n")
    body = _client(swarm).get("/logs/g/t").json()
    assert body["status"] == "ok"
    assert set(body) >= {"status", "system", "task_id", "meta", "messages"}


def test_logs_404_when_no_data(swarm):
    assert _client(swarm).get("/logs/g/no_such").status_code == 404


def test_logs_all_lists_swarm_tasks(swarm):
    threads, _ = swarm.paths
    _seed(threads, _sid("g", "a"), "completed", exit_code=0)
    logs = _client(swarm).get("/logs/all").json()["logs"]
    assert [(e["system"], e["task_id"]) for e in logs] == [("g", "a")]


# --- паритет с вебхуком -----------------------------------------------------


def test_same_requests_get_answers_of_the_same_shape(
    swarm, stub_runner, tmp_path, monkeypatch
):
    """Клиент переключается между webhook и swarm сменой адреса: ключи
    ответов и коды на общих ручках совпадают."""
    webhook = make_webhook(tmp_path)

    async def fake_start(self, system, task_id, message, trace_id):
        del message, trace_id
        self.registry.reserve(system=system, task_id=task_id)

    monkeypatch.setattr(type(webhook), "_start_run", fake_start)
    hook, sw = TestClient(webhook.app()), _client(swarm)
    body = {"task_id": "p1", "system": "s"}
    a, b = (
        hook.post("/events/webhook", json=body),
        sw.post("/events/webhook", json=body),
    )
    assert (a.status_code, b.status_code) == (200, 200)
    assert set(a.json()) == set(b.json())
    for path in ("/runs/s/none", "/logs/s/none"):
        assert hook.get(path).status_code == sw.get(path).status_code == 404
    reserved = {"task_id": "p2", "system": "acp"}
    assert (
        hook.post("/events/webhook", json=reserved).json()["detail"]["status"]
        == sw.post("/events/webhook", json=reserved).json()["detail"]["status"]
        == "error"
    )
    assert hook.get("/healthcheck").json() == sw.get("/healthcheck").json()


# --- остановка --------------------------------------------------------------


def _killpg_spy(monkeypatch):
    calls = {"term": [], "kill": []}

    def fake_killpg(pid, sig):
        import signal

        if sig == signal.SIGTERM:
            calls["term"].append(pid)
        elif sig == signal.SIGKILL:
            calls["kill"].append(pid)

    monkeypatch.setattr("os.killpg", fake_killpg)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)  # все живы

    async def fast_sleep(_t):
        return None

    monkeypatch.setattr("asyncio.sleep", fast_sleep)
    return calls


def test_stop_kills_active_sandboxes(swarm, monkeypatch):
    """Остановка: SIGTERM, пауза, SIGKILL живым; meta — server_shutdown."""
    threads, _ = swarm.paths
    sid = _sid("g", "alive")
    _seed(threads, sid, "running", pid=12345)
    swarm.monitor = MagicMock()
    swarm.monitor.get_active_ids_copy.return_value = {sid}
    calls = _killpg_spy(monkeypatch)
    monitor = swarm.monitor
    asyncio.run(swarm.astop())
    assert calls["term"] == [12345]
    assert calls["kill"] == [12345]
    meta = storage.load_swarm_meta(threads, sid)
    assert meta["swarm_meta"]["status"] == "unknown_failed"
    assert meta["swarm_meta"]["error_type"] == "server_shutdown"
    monitor.unregister_active.assert_called_once_with(sid)
    monitor.stop.assert_called_once()
    assert swarm.monitor is None


def test_stop_without_active_sandboxes(swarm):
    swarm.monitor = MagicMock()
    swarm.monitor.get_active_ids_copy.return_value = set()
    asyncio.run(swarm.astop())


def test_stop_without_monitor_is_a_noop(swarm):
    swarm.monitor = None
    asyncio.run(swarm.astop())
