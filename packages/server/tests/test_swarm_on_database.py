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
from server_harness import LOOPBACK_URL

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

    def fake_start(
        swarm_cfg, *, system, task_id, payload, trace_id=None, query=None
    ):
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
    resp = TestClient(surface.app(), base_url=LOOPBACK_URL).get(
        "/v1/tasks/g/hook/messages"
    )
    assert resp.status_code == 404


def test_message_into_a_running_task_is_queued_into_the_inbox(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    first = client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    assert first.json()["status"] == "started"
    again = client.post(
        "/v1/tasks",
        json={"system": "g", "task_id": "t1", "text": "и тесты"},
    )
    assert again.status_code == 202
    assert again.json()["status"] == "queued"
    entries = run_sync(surface.store.read_stream(KEY, "inbox"))
    assert "и тесты" in entries[-1].data["content"]
    assert len(surface.started) == 1


def test_task_thread_is_marked_as_swarm(surface):
    """Восстановление вебхука такой тред не берёт: его ведёт песочница."""
    TestClient(surface.app(), base_url=LOOPBACK_URL).post(
        "/v1/tasks", json={"system": "g", "task_id": "t1"}
    )
    record = run_sync(surface.store.get_thread(KEY))
    assert "swarm" in record.frontend


def test_logs_come_from_the_store(surface):
    client = TestClient(surface.app(), base_url=LOOPBACK_URL)
    client.post("/v1/tasks", json={"system": "g", "task_id": "t1"})
    run_sync(
        Threads(surface.store).append_messages(
            KEY, [{"id": "u1", "role": "user", "content": "из песочницы"}]
        )
    )
    body = client.get("/v1/tasks/g/t1/messages").json()
    assert [m["message"]["content"] for m in body["messages"]] == [
        "из песочницы"
    ]
    assert client.get("/v1/tasks/g/t1").json()["swarm"]["status"] == "running"


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


def test_swarm_does_not_take_over_a_webhook_thread(surface):
    """Тред вебхука с тем же (system, task_id) — чужой: swarm не помечает
    его своим и не запускает на нём песочницу (она читала бы и писала
    чужую историю)."""
    run_sync(
        Threads(surface.store).append_messages(
            KEY, [{"id": "w1", "role": "user", "content": "задача вебхука"}]
        )
    )
    resp = TestClient(surface.app(), base_url=LOOPBACK_URL).post(
        "/v1/tasks", json={"system": "g", "task_id": "t1", "message": "x"}
    )
    assert resp.status_code == 409
    assert surface.started == []
    record = run_sync(surface.store.get_thread(KEY))
    assert "swarm" not in record.frontend


def _swarm_with(tmp_path, threads: dict, sandbox: dict) -> SwarmSurface:
    from gc_core.stores import Stores

    loaded = make_settings(
        {
            "paths.threads_data_path": str(tmp_path / "files"),
            "storage": {"threads": threads},
        }
    )
    return SwarmSurface(
        ServerRuntime(
            base=loaded,
            config=ServerConfig.model_validate(
                {"swarm": {"storage": sandbox}}
            ),
            stores=Stores(threads=MemoryThreadStore(), agent=object()),
        ),
        swarm={
            "threads_dir": str(tmp_path),
            "sandbox_root_dir": str(tmp_path),
        },
    )


SERVER_PG = {"type": "postgres", "dsn": "postgresql://srv:s3cret@db/gc"}


@pytest.mark.parametrize(
    "sandbox",
    [
        {"type": "postgres", "dsn": "postgresql://box:other@db/gc"},
        {"type": "postgres", "dsn": "postgresql://box:other@DB:5432/gc"},
        {
            "type": "postgres",
            "dsn": "postgresql://box:other@db/gc",
            "schema": "public",
        },
    ],
    ids=["другая учётка", "порт по умолчанию", "схема по умолчанию"],
)
def test_sandbox_store_is_the_server_database(tmp_path, sandbox):
    surface = _swarm_with(tmp_path, SERVER_PG, sandbox)
    assert surface.swarm["thread_store"] == sandbox


@pytest.mark.parametrize(
    "sandbox",
    [
        {"type": "redis", "url": "redis://db:6379/0"},
        {"type": "postgres", "dsn": "postgresql://box:other@db/other"},
        {"type": "postgres", "dsn": "postgresql://box:other@db2/gc"},
        {"type": "postgres", "dsn": "postgresql://box:other@db:6432/gc"},
        {
            "type": "postgres",
            "dsn": "postgresql://box:other@db/gc",
            "schema": "sandbox",
        },
    ],
    ids=["другой тип", "другая база", "другой хост", "другой порт", "схема"],
)
def test_sandbox_store_elsewhere_is_a_start_error(tmp_path, sandbox):
    """Песочница писала бы тред не туда, откуда его читают /logs и очередь
    сообщений сервера. Учётные данные в тексте ошибки не показываются."""
    from gc_server.config import ServerConfigError

    with pytest.raises(ServerConfigError) as err:
        _swarm_with(tmp_path, SERVER_PG, sandbox)
    assert "s3cret" not in str(err.value)
    assert "other@" not in str(err.value)


@pytest.mark.parametrize(
    ("server", "sandbox", "same"),
    [
        (
            {"type": "redis", "url": "redis://:pw@cache:6379"},
            {"type": "redis", "url": "redis://:box@cache/0", "prefix": "gc:"},
            True,
        ),
        (
            {"type": "redis", "url": "redis://cache/0"},
            {"type": "redis", "url": "redis://cache/1"},
            False,
        ),
        (
            {"type": "s3", "bucket": "b", "endpoint_url": "http://s3:9000"},
            {"type": "s3", "bucket": "b", "endpoint_url": "http://s3:9000"},
            True,
        ),
        (
            {"type": "s3", "bucket": "b"},
            {"type": "s3", "bucket": "b", "prefix": "box/"},
            False,
        ),
        (
            {"type": "mysql", "dsn": "mysql://srv:pw@my/gc"},
            {"type": "mysql", "dsn": "mysql://box:pw@my:3306/gc"},
            True,
        ),
    ],
    ids=[
        "redis: префикс и номер по умолчанию",
        "redis: другой номер базы",
        "s3: тот же бакет",
        "s3: другой префикс",
        "mysql: порт по умолчанию",
    ],
)
def test_sandbox_store_location_per_type(tmp_path, server, sandbox, same):
    from gc_server.config import ServerConfigError

    if same:
        assert _swarm_with(tmp_path, server, sandbox).swarm["thread_store"]
    else:
        with pytest.raises(ServerConfigError):
            _swarm_with(tmp_path, server, sandbox)
