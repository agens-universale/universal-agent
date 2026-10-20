"""Swarm на общей базе (спека сервера, 13.7).

Хранилище тредов в сетевой базе: тред задачи swarm ведёт ``gc-cli`` в
песочнице (``-t``, ``--system``), ``/logs`` swarm-порта читает его из
хранилища, сообщение в идущую задачу ложится во входящий поток треда
(``queued``) — его забирает опрос ядра в песочнице. На файлах — как
раньше (409).
"""

import pytest
from fastapi.testclient import TestClient
from gc_core.threads.facade import Threads, run_sync
from gc_core.threads.models import ThreadKey
from gc_server.app import ServerRuntime
from gc_server.config import ServerConfig
from gc_server.swarm import storage
from gc_server.swarm.surface import SwarmSurface
from gc_testkit.memory_thread_store_v2 import MemoryThreadStore
from gc_testkit.settings import make_settings

KEY = ThreadKey(system="g", thread="t1")


@pytest.fixture
def surface(tmp_path, monkeypatch):
    threads_dir, sandbox = tmp_path / "threads", tmp_path / "sandbox"
    threads_dir.mkdir()
    sandbox.mkdir()
    store = MemoryThreadStore()
    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "files"),
            "storage": {
                "threads": {
                    "type": "postgres",
                    "dsn": "postgresql://u:pw@db/a",
                }
            },
        }
    )
    from gc_core.stores import Stores

    runtime = ServerRuntime(
        base=loaded,
        config=ServerConfig.model_validate(
            {
                "swarm": {
                    "storage": {
                        "type": "postgres",
                        "dsn": "postgresql://sandbox:pw@db/a",
                    }
                }
            }
        ),
        stores=Stores(threads=store, agent=object()),
    )
    surface = SwarmSurface(
        runtime,
        swarm={
            "threads_dir": str(threads_dir),
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
    )
    started: list = []

    def fake_start(swarm_cfg, *, system, task_id, payload, trace_id=None):
        sid = f"swarm_{system}_{task_id}"
        started.append(swarm_cfg)
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
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    surface.started = started
    surface.store = store
    return surface


def test_sandbox_gets_its_own_connection(surface):
    """Песочнице — подключение из swarm.storage (свои урезанные права), а не
    учётные данные сервера: агент в песочнице может их прочитать."""
    assert surface.swarm["thread_store"] == {
        "type": "postgres",
        "dsn": "postgresql://sandbox:pw@db/a",
    }


def test_without_swarm_storage_the_sandbox_stays_on_files(tmp_path):
    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "files"),
            "storage": {
                "threads": {
                    "type": "postgres",
                    "dsn": "postgresql://u:pw@db/a",
                }
            },
        }
    )
    from gc_core.stores import Stores

    surface = SwarmSurface(
        ServerRuntime(
            base=loaded,
            config=ServerConfig(),
            stores=Stores(threads=MemoryThreadStore(), agent=object()),
        ),
        swarm={
            "threads_dir": str(tmp_path),
            "sandbox_root_dir": str(tmp_path),
        },
    )
    assert "thread_store" not in surface.swarm


def test_logs_do_not_read_threads_of_other_tasks(surface):
    """Тред вебхука с тем же (system, task_id) — не задача swarm: /logs
    swarm-порта его не отдаёт."""
    run_sync(
        Threads(surface.store).append_messages(
            ThreadKey(system="g", thread="hook"),
            [{"id": "s1", "role": "user", "content": "секрет вебхука"}],
        )
    )
    resp = TestClient(surface.app()).get("/logs/g/hook")
    assert resp.status_code == 404


def test_message_into_a_running_task_is_queued_into_the_inbox(surface):
    client = TestClient(surface.app())
    first = client.post(
        "/events/webhook", json={"system": "g", "task_id": "t1"}
    )
    assert first.json()["status"] == "started"
    again = client.post(
        "/events/webhook",
        json={"system": "g", "task_id": "t1", "text": "и тесты"},
    )
    assert again.status_code == 200
    assert again.json()["status"] == "queued"
    entries = run_sync(surface.store.read_stream(KEY, "inbox"))
    assert "и тесты" in entries[-1].data["content"]
    assert len(surface.started) == 1


def test_task_thread_is_marked_as_swarm(surface):
    """Восстановление вебхука такой тред не берёт: его ведёт песочница."""
    TestClient(surface.app()).post(
        "/events/webhook", json={"system": "g", "task_id": "t1"}
    )
    record = run_sync(surface.store.get_thread(KEY))
    assert "swarm" in record.frontend


def test_logs_come_from_the_store(surface):
    client = TestClient(surface.app())
    client.post("/events/webhook", json={"system": "g", "task_id": "t1"})
    run_sync(
        Threads(surface.store).append_messages(
            KEY, [{"id": "u1", "role": "user", "content": "из песочницы"}]
        )
    )
    body = client.get("/logs/g/t1").json()
    assert [m["content"] for m in body["messages"]] == ["из песочницы"]
    assert body["meta"]["swarm_meta"]["status"] == "running"


def test_sqlite_stays_on_the_sandbox_files(tmp_path):
    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "files"),
            "storage": {
                "threads": {"type": "sqlite", "path": str(tmp_path / "t.db")}
            },
        }
    )
    surface = SwarmSurface(
        ServerRuntime(base=loaded, config=ServerConfig()),
        swarm={
            "threads_dir": str(tmp_path),
            "sandbox_root_dir": str(tmp_path),
        },
    )
    assert "thread_store" not in surface.swarm
